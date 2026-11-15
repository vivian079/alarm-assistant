FROM python:3.12-slim
WORKDIR /eval
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "run_eval.py"]
