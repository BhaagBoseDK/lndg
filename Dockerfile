FROM python:3
ENV PYTHONUNBUFFERED 1
#RUN git clone https://github.com/cryptosharks131/lndg.git /lndg
WORKDIR /lndg
COPY requirements.txt /lndg
RUN pip install -r requirements.txt
RUN pip install supervisor whitenoise
