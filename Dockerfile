# Minimal image for running a Sim bot.
#
# Build:
#   docker build -t simbot .
#
# Run the greedy bot:
#   docker run -e SIM_API_KEY="sim_..." simbot python examples/greedy_agent.py
#
# Run the Q-learning bot with persistent Q-table:
#   docker run -e SIM_API_KEY="sim_..." -v $(pwd)/qdata:/app/data \
#     -w /app/data simbot python /app/examples/q_learning_agent.py
FROM python:3.12-slim

# System deps for numpy wheels (no compiler needed for slim binaries)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package and install
COPY pyproject.toml README.md LICENSE ./
COPY simbot ./simbot
COPY examples ./examples

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[rl]"

# Don't run as root
RUN useradd -m -u 1000 simbot && chown -R simbot:simbot /app
USER simbot

# Default to greedy agent — override at runtime
CMD ["python", "examples/greedy_agent.py"]
