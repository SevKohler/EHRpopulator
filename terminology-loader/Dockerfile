FROM python:3.12-slim
RUN pip install requests
COPY load.py /load.py
ENTRYPOINT ["python", "/load.py"]
