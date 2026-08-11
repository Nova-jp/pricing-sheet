FROM python:3.11-slim

WORKDIR /app

COPY cloud/requirements.txt /app/cloud/requirements.txt
RUN pip install --no-cache-dir -r /app/cloud/requirements.txt

COPY swap_pricing /app/swap_pricing
COPY cloud/entrypoint.py /app/cloud/entrypoint.py

CMD ["python", "/app/cloud/entrypoint.py"]
