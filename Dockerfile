# The scorer builds this on linux/amd64 and runs it with no network. The digest is
# the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# What sqlglot's tests need, at the versions the commits were labeled with
# (requirements.lock of the starter kit).
RUN pip install --no-cache-dir --disable-pip-version-check \
    duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 python-dateutil==4.0.0 \
    pytz==2026.6 tzdata==2026.6 setuptools==84.2.1

# sqlglot at the pinned commit: patches are replayed against these sources.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /app/sg

# Maps of the pinned commit (tools/build_map.py, tools/prep.py, tools/time_tests.py,
# tools/record_duck.py).
COPY maps /app/maps
COPY selector.py runner.py /app/
RUN chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
