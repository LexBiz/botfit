module.exports = {
  apps: [
    {
      name: "botfit",
      cwd: "/var/botfit",
      script: "/var/botfit/.venv/bin/python",
      args: "-m src.bot",
      interpreter: "none",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      out_file: "/var/log/botfit/out.log",
      error_file: "/var/log/botfit/err.log",
      merge_logs: true,
      time: true,
    },
  ],
};

