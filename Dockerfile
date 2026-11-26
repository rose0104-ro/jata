FROM python:3.12-slim
WORKDIR /app
COPY index.html admin.html server.py ./
ENV PYTHONUNBUFFERED=1 JATA_FORMAL=1 JATA_DATA_DIR=/data
RUN mkdir -p /data && chmod 700 /data
EXPOSE 8765
CMD ["python", "server.py", "--lan"]
