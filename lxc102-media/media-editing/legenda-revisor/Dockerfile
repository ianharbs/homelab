FROM python:3.12-alpine
RUN pip install --no-cache-dir anthropic==2.8.0
WORKDIR /app
# O código vem montado em /app (./legenda-revisor), como no midia-notify:
# mudança só no revisor.py => docker restart legenda-revisor.
CMD ["python", "/app/revisor.py", "servico"]
