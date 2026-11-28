FROM python:3.13-alpine
RUN adduser -D -u 1000 app
WORKDIR /app
COPY app.py .
COPY static ./static
USER app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8080/cadastro/health || exit 1
CMD ["python", "-u", "app.py"]
