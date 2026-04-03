module.exports = {
  apps: [
    {
      name: "assistant",
      script: "/home/ubuntu/dev/assistant/.venv/bin/python",
      args: "main.py",
      cwd: "/home/ubuntu/dev/assistant",
      interpreter: "none",
      watch: false,
      restart_delay: 3000,
      max_restarts: 10,
      env: { PYTHONUNBUFFERED: "1", DEBUG_LOGGING: "true" },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "/home/ubuntu/.pm2/logs/assistant-error.log",
      out_file: "/home/ubuntu/.pm2/logs/assistant-out.log",
    },
  ],
};
