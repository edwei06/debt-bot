FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷貝程式
COPY app/ /app/

# 讓 python 直接把輸出 flush 出來，便於記錄
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
