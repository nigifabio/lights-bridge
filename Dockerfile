FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lights_bridge lights_bridge
ENV LIGHTS_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
CMD ["python", "-m", "lights_bridge"]
