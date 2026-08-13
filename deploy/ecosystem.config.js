module.exports = {
  apps: [
    {
      name: "ticket-tool",
      script: "main.py",
      interpreter: "python3",
      cwd: __dirname + "/..",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      cron_restart: "0 4 * * *",
      env: {
        TICKETBOT_AUTO_SYSTEMD: "0",
      },
    },
  ],
};
