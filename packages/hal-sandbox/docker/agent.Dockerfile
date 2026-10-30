# Agent image for `hal code`: python slim with hal-kit and hal-agent installed from the workspace.
# Built by `hal code --build` from a staged context holding packages/hal-kit, packages/hal-agent and the
# stdlib-only box-side scripts (boxside/); the entrypoint holds the lifeline and runs hal-agent as its child.
FROM python:3.12-slim
COPY hal-kit /src/hal-kit
COPY hal-agent /src/hal-agent
RUN pip install --no-cache-dir /src/hal-kit /src/hal-agent && rm -rf /src
COPY boxside/ /opt/hal/bin/
RUN chmod 0755 /opt/hal/bin/*.py && ln -s hal_box_entry.py /opt/hal/bin/hal-box-entry \
    && ln -s hal_relay.py /opt/hal/bin/hal-relay && ln -s hal_mcp_bridge.py /opt/hal/bin/hal-mcp-bridge
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["/opt/hal/bin/hal-box-entry"]
