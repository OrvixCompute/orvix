# systemd units

## Monthly burn reminder

`orvix-burn-reminder.timer` fires on the 1st of each month and runs
`orvix-burn-reminder.service`, which logs the ORVX currently held for burn. It
**does not** execute a burn — a human runs `python scripts/burn.py execute`
after reviewing.

Adjust `User=`, `WorkingDirectory=`, and `EnvironmentFile=` to your deployment,
then:

```bash
sudo cp orvix-burn-reminder.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orvix-burn-reminder.timer
systemctl list-timers orvix-burn-reminder.timer
```
