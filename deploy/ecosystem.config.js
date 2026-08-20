module.exports = {
  apps: [
    {
      name: "TicketV2",
      script: "main.py",
      interpreter: "python3",
      cwd: __dirname + "/..",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
      restart_delay: 5000,
      kill_timeout: 10000,
      max_memory_restart: "512M",
      cron_restart: "0 4 * * *",
      time: true,
      env: {
        TICKETBOT_AUTO_SYSTEMD: "0",
      },
    },
  ],
};