FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY spendflow spendflow
COPY static static
VOLUME /app/data
EXPOSE 8321
CMD ["uvicorn", "spendflow.app:app", "--host", "0.0.0.0", "--port", "8321"]
