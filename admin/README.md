# One-time setup on the ASTRUM machine (needs sudo)

## CPU power tile: keep the energy counter readable after every boot

The CPU package energy counter (`/sys/class/powercap/intel-rapl:0/energy_uj`, RAPL) is readable by root only, and the status service runs as an
ordinary user, so the **CPU power** tile says "unavailable". A plain `chmod` works until the next reboot and is then lost. This rule re-applies
it every time the counter appears at boot.

Open a terminal on the ASTRUM machine as a user with sudo and paste this single line (the dashboard's CPU power window has a Copy button for it):

```bash
echo 'ACTION=="add", SUBSYSTEM=="powercap", KERNEL=="intel-rapl:0", RUN+="/bin/chmod a+r /sys%p/energy_uj"' | sudo tee /etc/udev/rules.d/99-rapl-readable.rules && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=powercap --action=add && cat /sys/class/powercap/intel-rapl:0/energy_uj
```

It prints a number when it worked. Within 5 minutes the tile shows the watts, and the CPU power window then says a boot-time rule is installed.
The same rule is in `99-rapl-readable.rules` in this folder (`sudo cp 99-rapl-readable.rules /etc/udev/rules.d/`, then the `udevadm` lines).

After the next reboot, run `cat /sys/class/powercap/intel-rapl:0/energy_uj` as a normal user: a number means the rule did its job.

What was checked: `udevadm verify` accepts the rule on the machine, and its `%p` expands to the right file. It has not been run through a real reboot yet.

Security note: a world-readable energy counter lets any local user observe the CPU's power draw, which is the side channel Linux restricted it for
(PLATYPUS). On a machine whose users are trusted this is usually acceptable. To undo it, delete the rule and run
`sudo chmod o-r /sys/class/powercap/intel-rapl:0/energy_uj`.
