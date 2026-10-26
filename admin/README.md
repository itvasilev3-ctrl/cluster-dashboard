# One-time setup on the ASTRUM machine (needs sudo)

## CPU power tile: make the energy counter readable after every boot

The CPU package energy counter (`/sys/class/powercap/intel-rapl:0/energy_uj`, RAPL) is readable by root only. The status service runs as an
ordinary user, so without this the **CPU power** tile says "unavailable". A plain `chmod` is lost at the next reboot; this udev rule applies it
every time the counter appears at boot.

Run on the ASTRUM machine, from this folder:

```bash
sudo cp 99-rapl-readable.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=powercap --action=add     # apply now, without a reboot
cat /sys/class/powercap/intel-rapl:0/energy_uj                    # as a normal user: must print a number, not "Permission denied"
```

After the next reboot, run the last command again (without sudo) to confirm the rule worked; the dashboard tile fills in within 5 minutes.

Security note: a world-readable energy counter lets any local user observe the CPU's power draw, which is the side channel Linux restricted it
for (PLATYPUS). On a machine whose users are trusted this is usually acceptable; to undo it, delete the rule and run
`sudo chmod o-r /sys/class/powercap/intel-rapl:0/energy_uj`.

This rule has not been tested through a real reboot yet.
