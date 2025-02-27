import django, json, secrets, asyncio
from asgiref.sync import sync_to_async
from django.db.models import Sum, F
from datetime import datetime, timedelta
from gui.lnd_deps import lightning_pb2 as ln
from gui.lnd_deps import lightning_pb2_grpc as lnrpc
from gui.lnd_deps import router_pb2 as lnr
from gui.lnd_deps import router_pb2_grpc as lnrouter
from gui.lnd_deps.lnd_connect import lnd_connect, async_lnd_connect
from os import environ
from typing import List

environ['DJANGO_SETTINGS_MODULE'] = 'lndg.settings'
django.setup()
from gui.models import Rebalancer, Channels, LocalSettings, Forwards, Autopilot

@sync_to_async
def get_out_cans(rebalance, auto_rebalance_channels):
    try:
        return list(auto_rebalance_channels.filter(auto_rebalance=False, percent_outbound__gte=F('ar_out_target')).exclude(remote_pubkey=rebalance.last_hop_pubkey).values_list('chan_id', flat=True))
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error getting outbound cands: {str(e)=}")

@sync_to_async
def save_record(record):
    try:
        record.save()
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error saving database record: {str(e)=}")

@sync_to_async
def inbound_cans_len(inbound_cans):
    try:
        return len(inbound_cans)
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error getting inbound cands: {str(e)=}")

async def run_rebalancer(rebalance, worker):
    try:
        #Reduce potential rebalance value in percent out to avoid going below AR-OUT-Target
        #Only considering local balance for outbound_cans. This is conservative and avoids channel depleting beyond ar-out target if all out going htlcs settle. For inbound, pending inbound is considerred in remote balance.
        auto_rebalance_channels = Channels.objects.filter(is_active=True, is_open=True, private=False).annotate(percent_outbound=((Sum('local_balance')-rebalance.value)*100)/Sum('capacity')).annotate(inbound_can=(((Sum('remote_balance')+Sum('pending_inbound'))*100)/Sum('capacity'))/Sum('ar_in_target'))
        outbound_cans = await get_out_cans(rebalance, auto_rebalance_channels)
        if len(outbound_cans) == 0 and rebalance.manual == False:
            print (f"{datetime.now().strftime('%c')} : No outbound_cans")
            rebalance.status = 406
            rebalance.start = datetime.now()
            rebalance.stop = datetime.now()
            await save_record(rebalance)
            return None
        elif str(outbound_cans).replace('\'', '') != rebalance.outgoing_chan_ids and rebalance.manual == False:
            #print (f"{datetime.now().strftime('%c')} : Outbound chans are changed, replacing old {rebalance.outgoing_chan_ids=} with new {outbound_cans=}")
            rebalance.outgoing_chan_ids = str(outbound_cans).replace('\'', '')

        #Check if rebalance still required or if things changed while waiting..
        inbound_cans = auto_rebalance_channels.filter(remote_pubkey=rebalance.last_hop_pubkey).filter(auto_rebalance=True, inbound_can__gte=1)
        if await inbound_cans_len(inbound_cans) == 0 and rebalance.manual ==  False:
            print (f"{datetime.now().strftime('%c')} : AR Not Required ..")
            rebalance.status = 407
            rebalance.start = datetime.now()
            rebalance.stop = datetime.now()
            await save_record(rebalance)
            return None

        rebalance.start = datetime.now()
        try:
            #Open connection with lnd via grpc
            stub = lnrpc.LightningStub(lnd_connect())
            routerstub = lnrouter.RouterStub(async_lnd_connect())
            chan_ids = json.loads(rebalance.outgoing_chan_ids)
            timeout = rebalance.duration * 60
            invoice_response = stub.AddInvoice(ln.Invoice(value=rebalance.value, expiry=timeout))
            print (f"{datetime.now().strftime('%c')} : {worker=} starting rebalance for: {rebalance.target_alias=} {rebalance.last_hop_pubkey=} {rebalance.value=} {rebalance.duration=}")
            async for payment_response in routerstub.SendPaymentV2(lnr.SendPaymentRequest(payment_request=str(invoice_response.payment_request), fee_limit_msat=int(rebalance.fee_limit*1000), outgoing_chan_ids=chan_ids, last_hop_pubkey=bytes.fromhex(rebalance.last_hop_pubkey), timeout_seconds=(timeout-5), allow_self_payment=True), timeout=(timeout+60)):
                #print (f"{datetime.now().strftime('%c')} : {worker=} got a payment response: {payment_response.status=} {payment_response.failure_reason=} {payment_response.payment_hash=}")
                rebalance.payment_hash = payment_response.payment_hash
                if payment_response.status == 1 and rebalance.status == 0:
                    #IN-FLIGHT
                    rebalance.status = 1
                    await save_record(rebalance)
                elif payment_response.status == 2:
                    #SUCCESSFUL
                    rebalance.status = 2
                    rebalance.fees_paid = payment_response.fee_msat/1000
                    successful_out_htlcs = payment_response.htlcs
                elif payment_response.status == 3:
                    #FAILURE
                    if payment_response.failure_reason == 1:
                        #FAILURE_REASON_TIMEOUT
                        rebalance.status = 3
                    elif payment_response.failure_reason == 2:
                        #FAILURE_REASON_NO_ROUTE
                        rebalance.status = 4
                    elif payment_response.failure_reason == 3:
                        #FAILURE_REASON_ERROR
                        rebalance.status = 5
                    elif payment_response.failure_reason == 4:
                        #FAILURE_REASON_INCORRECT_PAYMENT_DETAILS
                        rebalance.status = 6
                    elif payment_response.failure_reason == 5:
                        #FAILURE_REASON_INSUFFICIENT_BALANCE
                        rebalance.status = 7
                elif payment_response.status == 0:
                    rebalance.status = 400
        except Exception as e:
            print (f"{datetime.now().strftime('%c')} : Exception: {str(e)=}")
            if str(e.code()) == 'StatusCode.DEADLINE_EXCEEDED':
                rebalance.status = 408
            else:
                rebalance.status = 400
                print (f"{datetime.now().strftime('%c')} : Error while sending payment: {str(e)=}")
        finally:
            rebalance.stop = datetime.now()
            await save_record(rebalance)
            print (f"{datetime.now().strftime('%c')} : {worker=} completed payment attempts for: {rebalance.target_alias=} {rebalance.last_hop_pubkey=} {rebalance.value=} {rebalance.payment_hash=} {rebalance.status=}")
            original_alias = rebalance.target_alias
            inc=1.21
            dec=2

            if rebalance.status ==2:
                await update_channels(stub, rebalance.last_hop_pubkey, successful_out_htlcs)
                #Reduce potential rebalance value in percent out to avoid going below AR-OUT-Target
                auto_rebalance_channels = Channels.objects.filter(is_active=True, is_open=True, private=False).annotate(percent_outbound=((Sum('local_balance')-rebalance.value*inc)*100)/Sum('capacity')).annotate(inbound_can=(((Sum('remote_balance')+Sum('pending_inbound'))*100)/Sum('capacity'))/Sum('ar_in_target'))
                inbound_cans = auto_rebalance_channels.filter(remote_pubkey=rebalance.last_hop_pubkey).filter(auto_rebalance=True, inbound_can__gte=1)
                rebalance.target_alias += ' ==> (' + str(int(payment_response.fee_msat/payment_response.value_msat*1000000)) + ')'
                await save_record(rebalance)

                if await inbound_cans_len(inbound_cans) > 0:
                    #Set with blank outgoing_chan_ids which are selected at run time.
                    next_rebalance = Rebalancer(value=int(rebalance.value*inc), fee_limit=round(rebalance.fee_limit*inc, 3), outgoing_chan_ids=' ', last_hop_pubkey=rebalance.last_hop_pubkey, target_alias=original_alias, duration=1)
                    await save_record(next_rebalance)
                    print (f"{datetime.now().strftime('%c')} : RapidFire up {next_rebalance.target_alias=} {next_rebalance.value=} {rebalance.value=}")
                else:
                    next_rebalance = None
            # For failed rebalances, try in rapid fire with reduced balances until give up.
            elif rebalance.status > 2 and rebalance.value > 69420:
                #Previous Rapidfire with increased value failed, try with lower value up to 69420.
                if rebalance.duration > 1:
                    next_value = await estimate_liquidity ( payment_response )
                    if next_value < 1000:
                        next_rebalance = None
                        return next_rebalance
                else:
                    next_value = rebalance.value/dec

                inbound_cans = auto_rebalance_channels.filter(remote_pubkey=rebalance.last_hop_pubkey).filter(auto_rebalance=True, inbound_can__gte=1)
                if await inbound_cans_len(inbound_cans) > 0:
                    next_rebalance = Rebalancer(value=int(next_value), fee_limit=round(rebalance.fee_limit/(rebalance.value/next_value), 3), outgoing_chan_ids=' ', last_hop_pubkey=rebalance.last_hop_pubkey, target_alias=original_alias, duration=1)
                    await save_record(next_rebalance)
                    print (f"{datetime.now().strftime('%c')} : RapidFire Down {next_rebalance.target_alias=} {next_rebalance.value=} {rebalance.value=}")
                else:
                    next_rebalance = None
            else:
                next_rebalance = None
            return next_rebalance
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error running rebalance attempt: {str(e)=}")

@sync_to_async
def estimate_liquidity( payment ):
    try:
        estimated_liquidity = 0
        if payment.status == 3:
            attempt = None
            for attempt in payment.htlcs:
                total_hops=len(attempt.route.hops)
                #Failure Codes https://github.com/lightningnetwork/lnd/blob/9f013f5058a7780075bca393acfa97aa0daec6a0/lnrpc/lightning.proto#L4200
                #print (f"{datetime.now().strftime('%c')} : DEBUG Liquidity Estimation {attempt.attempt_id=} {attempt.status=} {attempt.failure.code=} {attempt.failure.failure_source_index=} {total_hops=} {attempt.route.total_amt=} {payment.value_msat/1000=} {estimated_liquidity=} {payment.payment_hash=}")
                if attempt.failure.failure_source_index == total_hops:
                    #Failure from last hop indicating liquidity available
                    estimated_liquidity = attempt.route.total_amt if attempt.route.total_amt > estimated_liquidity else estimated_liquidity
                    chan_id=attempt.route.hops[len(attempt.route.hops)-1].chan_id
                    #print (f"{datetime.now().strftime('%c')} : DEBUG Liquidity Estimation {attempt.attempt_id=} {attempt.status=} {attempt.failure.code=} {chan_id=} {attempt.route.total_amt=} {payment.value_msat/1000=} {estimated_liquidity=} {payment.payment_hash=}")
        print (f"{datetime.now().strftime('%c')} : Estimated Liquidity {estimated_liquidity=} {payment.payment_hash=} {payment.status=} {payment.failure_reason=}")
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error estimating liquidity: {str(e)=}")
        estimated_liquidity = 0

    return estimated_liquidity

@sync_to_async
def update_channels(stub, incoming_channel, outgoing_channel_htlcs):
    try:
        # Incoming channel update
        for channel in stub.ListChannels(ln.ListChannelsRequest(peer=bytes.fromhex(incoming_channel))).channels:
            pending_in = 0
            pending_out = 0
            if len(channel.pending_htlcs) > 0:
                for htlc in channel.pending_htlcs:
                    if htlc.incoming == True:
                        pending_in += htlc.amount
                    else:
                        pending_out += htlc.amount
            db_channel = Channels.objects.filter(chan_id=channel.chan_id)[0]
            db_channel.pending_outbound = pending_out
            db_channel.pending_inbound = pending_in
            db_channel.local_balance = channel.local_balance
            db_channel.remote_balance = channel.remote_balance
            db_channel.save()
            print (f"{datetime.now().strftime('%c')} : Incoming Channel Update {channel.chan_id=} {int((channel.local_balance+pending_out)/channel.capacity*100)}% {channel.local_balance=} {pending_out=} {channel.remote_balance=} {pending_in=} {incoming_channel=}")
        # Outgoing channel update. It can be potentially MPP with multuple HTLCs (some success some failed) and multiple outgoing channels.
        processed_channels = []
        for htlc_attempt in outgoing_channel_htlcs:
            if htlc_attempt.status == 1:
                outgoing_channel = htlc_attempt.route.hops[0].pub_key
                outgoing_chan_id = htlc_attempt.route.hops[0].chan_id
                #MPP can pass via same outgoing channel at times, skip when already processed.
                if outgoing_chan_id in processed_channels:
                    #Already processed, continue
                    #print (f"{datetime.now().strftime('%c')} : Skipping .... {channel.chan_id=} in {processed_channels=}")
                    continue
                processed_channels.append(outgoing_chan_id)
                #Take care of multiple channels for same pub key
                for channel in stub.ListChannels(ln.ListChannelsRequest(peer=bytes.fromhex(outgoing_channel))).channels:
                    pending_in = 0
                    pending_out = 0
                    if channel.chan_id == outgoing_chan_id:
                        if len(channel.pending_htlcs) > 0:
                            for htlc in channel.pending_htlcs:
                                if htlc.incoming == True:
                                    pending_in += htlc.amount
                                else:
                                    pending_out += htlc.amount
                        db_channel = Channels.objects.filter(chan_id=channel.chan_id)[0]
                        db_channel.pending_outbound = pending_out
                        db_channel.pending_inbound = pending_in
                        db_channel.local_balance = channel.local_balance
                        db_channel.remote_balance = channel.remote_balance
                        db_channel.save()
                        print (f"{datetime.now().strftime('%c')} : Outgoing Channel Update {channel.chan_id=} {int((channel.local_balance)/channel.capacity*100)}% {channel.local_balance=} {pending_out=} {channel.remote_balance=} {pending_in=} {outgoing_channel=}")
                    #else:
                        #print (f"{datetime.now().strftime('%c')} : chan id different {channel.chan_id=} {outgoing_chan_id=}")
            #else:
                #print (f"{datetime.now().strftime('%c')} : Failed HTLC {htlc_attempt.status=} {htlc_attempt.route.hops[0].pub_key=}")
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error updating channel balances: {str(e)=}")

@sync_to_async
def auto_schedule() -> List[Rebalancer]:
    try:
        #No rebalancer jobs have been scheduled, lets look for any channels with an auto_rebalance flag and make the best request if we find one
        to_schedule = []
        if LocalSettings.objects.filter(key='AR-Enabled').exists():
            enabled = int(LocalSettings.objects.filter(key='AR-Enabled')[0].value)
        else:
            LocalSettings(key='AR-Enabled', value='0').save()
            enabled = 0
        if enabled == 0:
            return []
        auto_rebalance_channels = Channels.objects.filter(is_active=True, is_open=True, private=False).annotate(percent_outbound=((Sum('local_balance'))*100)/Sum('capacity')).annotate(inbound_can=(((Sum('remote_balance')+Sum('pending_inbound'))*100)/Sum('capacity'))/Sum('ar_in_target'))
        if len(auto_rebalance_channels) == 0:
            return []
        if not LocalSettings.objects.filter(key='AR-Outbound%').exists():
            LocalSettings(key='AR-Outbound%', value='75').save()
        if not LocalSettings.objects.filter(key='AR-Inbound%').exists():
            LocalSettings(key='AR-Inbound%', value='100').save()
        already_scheduled = Rebalancer.objects.exclude(last_hop_pubkey='').filter(status=0).values_list('last_hop_pubkey')
        inbound_cans = auto_rebalance_channels.filter(auto_rebalance=True, inbound_can__gte=1).exclude(remote_pubkey__in=already_scheduled).order_by('-inbound_can')
        if len(inbound_cans) == 0:
            return []
        if LocalSettings.objects.filter(key='AR-MaxFeeRate').exists():
            max_fee_rate = int(LocalSettings.objects.filter(key='AR-MaxFeeRate')[0].value)
        else:
            LocalSettings(key='AR-MaxFeeRate', value='100').save()
            max_fee_rate = 100
        if LocalSettings.objects.filter(key='AR-Variance').exists():
            variance = int(LocalSettings.objects.filter(key='AR-Variance')[0].value)
        else:
            LocalSettings(key='AR-Variance', value='0').save()
            variance = 0
        if LocalSettings.objects.filter(key='AR-WaitPeriod').exists():
            wait_period = int(LocalSettings.objects.filter(key='AR-WaitPeriod')[0].value)
        else:
            LocalSettings(key='AR-WaitPeriod', value='30').save()
            wait_period = 30
        if not LocalSettings.objects.filter(key='AR-Target%').exists():
            LocalSettings(key='AR-Target%', value='5').save()
        if not LocalSettings.objects.filter(key='AR-MaxCost%').exists():
            LocalSettings(key='AR-MaxCost%', value='65').save()
        for target in inbound_cans:
            target_fee_rate = int(target.local_fee_rate * (target.ar_max_cost/100))
            if target_fee_rate > 0 and target_fee_rate > target.remote_fee_rate:
                target_value = int(target.ar_amt_target+(target.ar_amt_target*((secrets.choice(range(-1000,1001))/1000)*variance/100)))
                target_fee = round(target_fee_rate*target_value*0.000001, 3) if target_fee_rate <= max_fee_rate else round(max_fee_rate*target_value*0.000001, 3)
                if target_fee == 0:
                    continue
                if LocalSettings.objects.filter(key='AR-Time').exists():
                    target_time = int(LocalSettings.objects.filter(key='AR-Time')[0].value)
                else:
                    LocalSettings(key='AR-Time', value='5').save()
                    target_time = 5
                # TLDR: willing to pay 1 sat for every value_per_fee sats moved
                if Rebalancer.objects.filter(last_hop_pubkey=target.remote_pubkey).exclude(status=0).exists():
                    last_rebalance = Rebalancer.objects.filter(last_hop_pubkey=target.remote_pubkey).exclude(status=0).order_by('-id')[0]
                    if not (last_rebalance.status == 2 or (last_rebalance.status > 2 and (int((datetime.now() - last_rebalance.stop).total_seconds() / 60) > wait_period)) or (last_rebalance.status == 1 and ((int((datetime.now() - last_rebalance.start).total_seconds() / 60) - last_rebalance.duration) > wait_period))):
                        continue
                print(f"{datetime.now().strftime('%c')} : Creating Auto Rebalance Request for: {target.chan_id=} {target.alias=} {target_value=} / {target.ar_amt_target=} {target_time=}")
                #print(f"{datetime.now().strftime('%c')} : Request routing through: {outbound_cans}")
                #print(f"{datetime.now().strftime('%c')} : {target_value} / {target.ar_amt_target}")
                #print(f"{datetime.now().strftime('%c')} : {target_fee}")
                #print(f"{datetime.now().strftime('%c')} : {target_time}")
                new_rebalance = Rebalancer(value=target_value, fee_limit=target_fee, outgoing_chan_ids=' ', last_hop_pubkey=target.remote_pubkey, target_alias=target.alias, duration=target_time)
                new_rebalance.save()
                to_schedule.append(new_rebalance)
        return to_schedule
    except Exception as e:
        print(f"{datetime.now().strftime('%c')} : Error scheduling rebalances: {str(e)}")
        return to_schedule

@sync_to_async
def auto_enable():
    try:

        if LocalSettings.objects.filter(key='AR-Autopilot').exists():
            enabled = int(LocalSettings.objects.filter(key='AR-Autopilot')[0].value)
        else:
            LocalSettings(key='AR-Autopilot', value='0').save()
            enabled = 0
        if LocalSettings.objects.filter(key='AR-APDays').exists():
            apdays = int(LocalSettings.objects.filter(key='AR-APDays')[0].value)
        else:
            LocalSettings(key='AR-APDays', value='7').save()
            apdays = 7
        if enabled == 1:
            lookup_channels=Channels.objects.filter(is_active=True, is_open=True, private=False)
            #Channel balane is not considerred changed until settled (pending balance is added to respective side as if not pending). This is to avoid flip flops enable/disable.
            channels = lookup_channels.values('remote_pubkey').annotate(outbound_percent=((Sum('local_balance')+Sum('pending_outbound'))*1000)/Sum('capacity')).annotate(inbound_percent=((Sum('remote_balance')+Sum('pending_inbound'))*1000)/Sum('capacity')).order_by()
            filter_day = datetime.now() - timedelta(days=apdays)
            forwards = Forwards.objects.filter(forward_date__gte=filter_day)
            for channel in channels:
                outbound_percent = int(round(channel['outbound_percent']/10, 0))
                inbound_percent = int(round(channel['inbound_percent']/10, 0))
                chan_list = lookup_channels.filter(remote_pubkey=channel['remote_pubkey']).values('chan_id')
                routed_in_apday = forwards.filter(chan_id_in__in=chan_list).count()
                routed_out_apday = forwards.filter(chan_id_out__in=chan_list).count()
                #Minimum 1M units for rebalance
                iapD = 0 if routed_in_apday == 0 else int(forwards.filter(chan_id_in__in=chan_list).aggregate(Sum('amt_in_msat'))['amt_in_msat__sum']/1000000000)/1
                oapD = 0 if routed_out_apday == 0 else int(forwards.filter(chan_id_out__in=chan_list).aggregate(Sum('amt_out_msat'))['amt_out_msat__sum']/1000000000)/1

                for peer_channel in lookup_channels.filter(chan_id__in=chan_list):
                    #print('Processing: ', peer_channel.alias, ' : ', peer_channel.chan_id, ' : ', oapD, " : ", iapD, ' : ', outbound_percent, ' : ', inbound_percent)
                    if peer_channel.ar_out_target == 100 and peer_channel.auto_rebalance == True:
                        #Special Case for LOOP, Wos, etc. Always Auto Rebalance if enabled to keep outbound full.
                        #print (f"{datetime.now().strftime('%c')} : Skipping AR enabled and 100% oTarget channel... {peer_channel.alias=} {peer_channel.chan_id=}")
                        pass
                    elif oapD > (iapD*1.10) and outbound_percent > 69:
                        #print('Case 1: Pass')
                        pass
                    elif oapD > (iapD*1.10) and inbound_percent > 69 and peer_channel.auto_rebalance == False:
                        #print('Case 2: Enable AR - o7D > i7D AND Inbound Liq > inbound trigger')
                        peer_channel.auto_rebalance = True
                        peer_channel.save()
                        Autopilot(chan_id=peer_channel.chan_id, peer_alias=peer_channel.alias, setting=(f"Enabled [2:{iapD}:{oapD}]"), old_value=0, new_value=1).save()
                        print (f"{datetime.now().strftime('%c')} : Auto Pilot Enabled(2): {peer_channel.alias=} {peer_channel.chan_id=} {oapD=} {iapD=} {inbound_percent=}")
                    elif oapD <= (iapD*1.10) and outbound_percent > 69 and peer_channel.auto_rebalance == True:
                        #print('Case 3: Disable AR - o7D < i7D AND Outbound Liq > outbound trigger')
                        peer_channel.auto_rebalance = False
                        peer_channel.save()
                        Autopilot(chan_id=peer_channel.chan_id, peer_alias=peer_channel.alias, setting=(f"Enabled [3:{iapD}:{oapD}]"), old_value=1, new_value=0).save()
                        print (f"{datetime.now().strftime('%c')} : Auto Pilot Disabled(3) {peer_channel.alias=} {peer_channel.chan_id=} {oapD=} {iapD=} {outbound_percent=}")
                    elif oapD == 0 and outbound_percent > 21 and peer_channel.auto_rebalance == True:
                        #print('Case 4: Disable AR - o7D = 0 i7D = 0 no routing and outbound > toggle off trigger')
                        peer_channel.auto_rebalance = False
                        peer_channel.save()
                        Autopilot(chan_id=peer_channel.chan_id, peer_alias=peer_channel.alias, setting=(f"Enabled [4:{iapD}:{oapD}]"), old_value=1, new_value=0).save()
                        print (f"{datetime.now().strftime('%c')} : Auto Pilot Disabled(4) {peer_channel.alias=} {peer_channel.chan_id=} {oapD=} {iapD=} {outbound_percent=}")
                    elif oapD == 0 and iapD == 0 and outbound_percent < 11 and peer_channel.auto_rebalance == False:
                        #print('Case 5: Enable AR - o7D = 0 i7D = 0 no routing and outbound < toggle on trigger')
                        peer_channel.auto_rebalance = True
                        peer_channel.save()
                        Autopilot(chan_id=peer_channel.chan_id, peer_alias=peer_channel.alias, setting=(f"Enabled [5:{iapD}:{oapD}]"), old_value=0, new_value=1).save()
                        print (f"{datetime.now().strftime('%c')} : Auto Pilot Enabled(5) {peer_channel.alias=} {peer_channel.chan_id=} {oapD=} {iapD=} {outbound_percent=}")
                    elif oapD <= (iapD*1.10) and inbound_percent > 69:
                        #print('Case 6: Pass')
                        pass
                    else:
                        #print('Case 7: Pass catch all')
                        pass
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error during auto channel enabling: {str(e)=}")

@sync_to_async
def get_scheduled_rebal(id):
    try:
        return Rebalancer.objects.get(id=id)
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error getting scheduled rebalances: {str(e)=}")

@sync_to_async
def get_pending_rebals():
    try:
        rebalances = Rebalancer.objects.filter(status=0).order_by('id')
        return rebalances, len(rebalances)
    except Exception as e:
        print (f"{datetime.now().strftime('%c')} : Error getting pending rebalances: {str(e)=}")

shutdown_rebalancer = False
scheduled_rebalances = []
active_rebalances = []
async def async_queue_manager(rebalancer_queue):
    global scheduled_rebalances, active_rebalances, shutdown_rebalancer
    print(f"{datetime.now().strftime('%c')} : Queue manager is starting...")
    try:
        while True:
            print(f"{datetime.now().strftime('%c')} : Queue currently has {rebalancer_queue.qsize()} items...")
            print(f"{datetime.now().strftime('%c')} : There are currently {len(active_rebalances)} tasks in progress...")
            print(f"{datetime.now().strftime('%c')} : Queue manager is checking for more work...")
            pending_rebalances, rebal_count = await get_pending_rebals()
            if rebal_count > 0:
                for rebalance in pending_rebalances:
                    if rebalance.id not in (scheduled_rebalances + active_rebalances):
                        print(f"{datetime.now().strftime('%c')} : Found a pending job to schedule with id: {rebalance.id}")
                        scheduled_rebalances.append(rebalance.id)
                        await rebalancer_queue.put(rebalance)
            await auto_enable()
            scheduled = await auto_schedule()
            if len(scheduled) > 0:
                print(f"{datetime.now().strftime('%c')} : Scheduling {len(scheduled)} more jobs...")
                for rebalance in scheduled:
                    scheduled_rebalances.append(rebalance.id)
                    await rebalancer_queue.put(rebalance)
            elif rebalancer_queue.qsize() == 0 and len(active_rebalances) == 0:
                print(f"{datetime.now().strftime('%c')} : Queue is still empty, stoping the rebalancer...")
                shutdown_rebalancer = True
                return
            await asyncio.sleep(69)
    except Exception as e:
        print(f"{datetime.now().strftime('%c')} : Queue manager exception: {str(e)}")
        shutdown_rebalancer = True
    finally:
        print (f"{datetime.now().strftime('%c')} : Queue manager has shut down...")

async def async_run_rebalancer(worker, rebalancer_queue):
    while True:
        global scheduled_rebalances, active_rebalances, shutdown_rebalancer
        if not rebalancer_queue.empty():
            rebalance = await rebalancer_queue.get()
            print (f"{datetime.now().strftime('%c')} : {worker=} is starting a new request... {rebalance.id=} {rebalance.target_alias=}")
            active_rebalance_id = None
            if rebalance != None:
                active_rebalance_id = rebalance.id
                active_rebalances.append(active_rebalance_id)
                scheduled_rebalances.remove(active_rebalance_id)
            while rebalance != None:
                rebalance = await run_rebalancer(rebalance, worker)
            if active_rebalance_id != None:
                active_rebalances.remove(active_rebalance_id)
            print (f"{datetime.now().strftime('%c')} : {worker=} completed its request...")
        else:
            if shutdown_rebalancer == True:
                return
        await asyncio.sleep(69)

async def start_queue(worker_count=1):
    rebalancer_queue = asyncio.Queue()
    manager = asyncio.create_task(async_queue_manager(rebalancer_queue))
    workers = [asyncio.create_task(async_run_rebalancer("Worker " + str(worker_num+1), rebalancer_queue)) for worker_num in range(worker_count)]
    await asyncio.gather(manager, *workers)
    print (f"{datetime.now().strftime('%c')} : Manager and workers have stopped...")

def main():
    if Rebalancer.objects.filter(status=1).exists():
        unknown_errors = Rebalancer.objects.filter(status=1)
        for unknown_error in unknown_errors:
            unknown_error.status = 400
            unknown_error.stop = datetime.now()
            unknown_error.save()
    if LocalSettings.objects.filter(key='AR-Workers').exists():
        worker_count = int(LocalSettings.objects.filter(key='AR-Workers')[0].value)
    else:
        LocalSettings(key='AR-Workers', value='1').save()
        worker_count = 1
    asyncio.run(start_queue(worker_count))
    print (f"{datetime.now().strftime('%c')} : Rebalancer successfully shutdown...")

if __name__ == '__main__':
    main()
