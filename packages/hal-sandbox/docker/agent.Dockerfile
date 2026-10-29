# Agent image for `hal code`: python slim with hal-kit and hal-agent installed from the workspace.
# Built by `hal code --build` from a staged context holding packages/hal-kit and packages/hal-agent.
FROM python:3.12-slim
COPY hal-kit /src/hal-kit
COPY hal-agent /src/hal-agent
RUN pip install --no-cache-dir /src/hal-kit /src/hal-agent && rm -rf /src
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["hal-agent"]
