# Serves the ingest routes and the MCP endpoint from one process.
# The data layer is pure standard library, so there is nothing to compile and
# no system packages to install.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY mcp_server/ ./mcp_server/

# Persist this: it holds every sample, the sleep history and the wrist-temperature
# baseline. On a container swap an unmounted data directory takes the lot with it.
ENV HEALTH_DATA_DIR=/app/data/health
VOLUME ["/app/data"]

# Overridden by most platforms — app.py reads $PORT and binds whatever it finds,
# because binding a hardcoded port while the router targets another one produces
# a service that looks healthy and 502s every request.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "server/app.py"]
