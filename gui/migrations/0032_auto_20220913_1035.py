# Generated by Django 3.2.7 on 2022-09-13 10:35

from django.db import migrations, models

def migrate_close_fees(apps, schedma_editor):
    channels = apps.get_model('gui', 'channels')
    closures = apps.get_model('gui', 'closures')
    close_fees = channels.objects.filter(closing_costs__gt=0)
    for close_fee in close_fees:
        closure = closures.objects.filter(funding_txid=close_fee.funding_txid, funding_index=close_fee.output_index)[0] if closures.objects.filter(funding_txid=close_fee.funding_txid, funding_index=close_fee.output_index).exists() else None
        if closure:
            closure.closing_costs = close_fee.closing_costs
            closure.save()

def revert_close_fees(apps, schedma_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('gui', '0031_pendingchannels'),
    ]

    operations = [
        migrations.AddField(
            model_name='closures',
            name='closing_costs',
            field=models.IntegerField(default=0),
        ),
        migrations.RunPython(migrate_close_fees, revert_close_fees),
        migrations.RemoveField(
            model_name='channels',
            name='closing_costs',
        ),
    ]
