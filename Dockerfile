# The scorer builds this on linux/amd64 and runs it with no network. The digest is
# the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# sqlglot at the pinned commit: the selector applies each patch to these sources.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /app/sg

# Dependency maps of the pinned commit (see tools/build_map.py, tools/prep.py).
COPY maps /app/maps
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
