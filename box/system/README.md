# System files of a Viindoo IoT Box

The files of this directory go to the path they have here, on the box. `apply.sh` installs
them: once when the box software is installed, then after each update of that software. It
must run after the files Odoo Community ships for a box are in place, because the web server
site replaces the one that comes with them.

    sudo box/system/apply.sh

Running it again changes nothing. It copies a file only when it differs, writes files of
`/etc` to the card as well as to the copy the running system reads, leaves the card read-only
if it found it so, and fails without installing anything when the roll-back guard of the
release cannot run. It fails too when the web server refuses the configuration it installed.
On a running system it restarts the Wi-Fi watch when the program or the unit of the watch
changed, and starts it otherwise, so that the watch runs the program of the release installed -
after an update or a roll-back - without waiting for the next start of the box. It starts and
stops no other service.

| File | Role |
|---|---|
| `etc/nginx/sites-enabled/default` | The web server site: one block for the address and host name of the box, one per other name the box holds |
| `etc/nginx/viindoo/box.conf` | Every rule of the web server, included by every block |
| `etc/nginx/viindoo/name.conf.template` | What the block of one more name is made of |
| `usr/share/viindoo-iot/box-down.html` | The page shown while the box service does not answer |
| `etc/systemd/system/odoo.service.d/viindoo.conf` | Runs the guard before each start, and after each stop, of the box service, and bounds the name look-ups of that service to about 4 seconds per name server |
| `usr/local/sbin/viin-box-guard` | The roll-back guard |
| `etc/avahi/services/viindoo-iot.service` | Announces the page of the box on the local network |
| `etc/systemd/system/viin-box-first-start.service`, `usr/local/sbin/viin-box-first-start` | Give a new box its own master password and host name |
| `etc/systemd/system/viin-box-wifi.service`, `usr/local/sbin/viin-box-wifi` | Join the Wi-Fi network the box keeps, or wait for the address of a cable, before the access point of the box is decided on |
| `etc/systemd/system/viin-box-wifi-watch.service` | Bring the box back to the Wi-Fi network it keeps while it runs, from its access point too; stop the access point once the box has an address on another network |
| `etc/systemd/system/hostapd.service.d/viindoo.conf`, `usr/local/sbin/viin-box-access-point-name` | Name the access point of the box `Viindoo-IoT-` and the hardware address of its Wi-Fi interface |

## Web server

The box answers with TLS 1.2 and 1.3. By its address or its host name it presents the
certificate of the box, the pair `nginx-cert.crt` and `nginx-cert.key`; that block never
changes, so a PC that accepted the box once is not asked again when another name arrives.

A name issued to the box is served by a block of its own. Whoever installs the name fills
`@NAME@` in `/etc/nginx/viindoo/name.conf.template`, saves the result in
`/etc/nginx/viindoo/names/` under a name that ends in `.conf` - in `/etc` and on the card -
puts the certificate and its key in the directory `/etc/ssl/private/viindoo-name/` - in
`/etc` and on the card - and has the web server read its configuration again. Removing the
file removes the name. A file of that directory whose name does not end in `.conf` is not read,
so a file may be written there under another name first and renamed when it is whole.

The pair of the name lives in that directory, which only root can enter, never beside the
trusted certificates. Each pair is written whole into a directory of its own,
`g-<16 hexadecimal digits>`, as `fullchain.pem` (the certificate, then its chain) and
`privkey.pem` (the key alone); the link `current` names the pair in use, and the web server
reads both files through it. A pair takes its place by one rename of that link, so after a
power cut the box serves the previous pair or the new one, never a key with the certificate
of another. The directories of pairs no longer in use are removed once the new one is served,
or at the next start.

Rules live in `box.conf` only. They add one header to the answers of the box service:
`Access-Control-Allow-Private-Network`, on the request by which a browser asks whether a
public page may call a device of a private network, and only when the box service itself
answers other web sites on that route; the static files of the modules carry the same
header. They also replace the cache lifetime that core gives the static files of every
module (one week) with `Cache-Control: no-cache`: a browser asks the box service before each
use, gets `304 Not Modified` for a file that did not change, and so never builds the page
from the files of two releases after an update. Each request handed on to the box service
carries `X-Real-IP`, the address of the device that asked, set by the web server in place of any
the browser sent; the box service believes it only from the web server itself.

Without a certificate, on port 80, the box serves a request only when it names an address or
the host name of the box; any other name is sent on to the address the request arrived at.
With a certificate the name is checked by the browser, against the certificate.

## First start

Every card is written from one image, so the first start gives the box what must not be shared.
It does so before the box service starts, and once: the file `/home/pi/viin-box-first-start-done`
says it is done, and a later start changes nothing.

- The master password of the box service. The image carries an empty one in
  `/home/pi/odoo.conf`, which forbids every use of the database pages; the first start writes a
  random one of its own in its place. It is written into the `[options]` section, the one Odoo
  reads, even when the box has since saved settings of its own in a section after it. It is shown
  nowhere: not in the log, not on a command line. The file keeps its owner and its mode, and stays
  readable by its owner only. `install_layer.sh` writes the empty value only when the `[options]`
  section holds none, so running it again never takes a box's password away. On a stock system
  prepared with `install_on_raspios.sh` the first start happens at the restart that ends the
  installation. This step needs no serial number.
- The host name. A new box names itself `viindoo-iot-` followed by the last four hexadecimal
  digits of the serial number of its board, before anything announces or uses the name. The name
  is given once, at the first start, and nothing on the box changes it later; the name people use
  is the Box Name on the server. A board that gives no serial number keeps the name it has.

To run the script on a copy of a box, set `VIIN_BOX_ROOT` to the directory that holds the
copy: every path of the box (`home/pi`, `etc`, `sys`, `root_bypass_ramdisks`) is then read
under it, and the host name of the machine is left alone. The software box does not run the
first start.

## Wi-Fi

NetworkManager joins a Wi-Fi network and holds it, with its password, as the profile
`viindoo-wifi`, in a file only root reads. The box page hands `nmcli` the name as the value of a
setting and the password on its standard input, so that neither the log of the commands run as
root nor the list of processes shows the password; the box writes neither into its
configuration. A name no access point announces is joined as a hidden network, and joined again
as one. `/etc` lives in memory, so the box keeps a network by copying that file to the card,
under `/root_bypass_ramdisks/etc/NetworkManager/system-connections/viindoo-wifi.nmconnection`
(or `/root_bypass_ramdisks/etc/netplan/90-NM-viindoo-wifi.yaml` on a system where netplan holds
the profiles of NetworkManager). A network is kept only once the box has joined it. "Forget the
saved network" removes the copy; a join asked before it keeps nothing, even when it ends later.

At each start `viin-box-wifi` brings that network up before `rc.local` runs core's
`wireless_ap.sh`, which starts the access point of the box only when the box has no address: the
access point starts when no network kept can be joined, never because the box looked too early.
A box that NetworkManager has joined to the network kept already is left on it. On a box that
keeps no network, core's script waits for no address at all; there the program waits, up to thirty
seconds, for the router to give the cable plugged in its address, so that the open access point
does not start beside the cable. A box without a cable is not held back. The program never holds
the start of the box back for more than about a minute, and never fails it.

`viin-box-wifi watch` (the unit `viin-box-wifi-watch.service`) then brings the box back to the
network it keeps, by itself:

- Off that network and off its access point - after a router restarts, or a weak signal - the box
  joins it again after one minute, then more and more seldom, at least every five minutes.
  NetworkManager alone would stop trying a network whose handshake failed, until someone asks.
- After ten minutes without any address, the access point starts, so that a person can set the
  box up: a router that restarts is back long before. This holds on a box that keeps no network
  too, which a cable pulled out leaves without an address; an address, a cable's, starts the ten
  minutes anew.
- On its access point - after a power cut that brought the router up after the box, or a join
  that failed - the box listens for the network kept every minute, asking for it by its name, so
  that a hidden network answers too. When it hears it, or when its radio cannot listen while it
  serves the access point, the box leaves the access point, joins the network and restarts the box
  service, as a join from the box page does; when that join fails, the access point starts again,
  and the next try waits longer, up to fifteen minutes. A radio may hear nothing while it serves
  the access point, even of a network in reach: a network not heard for fifteen minutes is tried
  anyway, at most every fifteen minutes.
- A box that gets an address on another network while its access point serves - a cable plugged
  in, or a router slower than the start of the box - stops the access point once nobody sets the
  box up through it, as core's script decides at each start: a box with an address has no access
  point. This holds on a box that keeps no network too; such a box neither listens for nor joins a
  network from its access point.
- It never takes the radio from a person setting the box up through the access point: it neither
  listens nor joins while such a person may be there, and asks again once it has listened, just
  before it leaves. A device connected to the access point counts as such a person for ten minutes
  after the watch first saw it, the time to open the box page, then for ten minutes after each
  use of the box page from its address. The box page and its log page read the box regularly
  while they are shown, and the box service notes each read, with the address of the device, in
  `/run/odoo/viin-page-use.json`, whether the page came through the web server or straight to the
  box service on port 8069, the address core's status ticket prints. A phone or tablet that
  joined the open access point by itself, and only stays on it, holds the box off its network no
  longer, even when it drops off the access point for a moment and connects again: a device keeps
  the time the watch first saw it until it has been away for ten minutes. On a radio that cannot
  tell who is connected, a device that holds an address the access point lent, which has not
  ended, stands for one; where that record cannot be read, every device connected counts.

While the access point serves, NetworkManager does not join the network kept by itself, which
would take the radio from the access point; that change is made in `/etc` only, so the next start
tries the network as always. The program and the joins asked from the box page take turns
(`/run/viin-box-wifi.lock`): a join holds the lock from the moment it is asked until it ends, and
the program does nothing meanwhile; the program holds it until the access point it starts, or the
restart of the box service it asks for, is over, and a join asked meanwhile is not started: the
page says the box is busy, and the join may be asked again a minute later. The program also holds
the lock for a moment at each look, even when it changes nothing: a join asked then waits a few
seconds for it before the page is told so. A network forgotten is
no longer looked for. A stop of the watch, as `apply.sh` asks after an update, waits for the look
it is in to end.

## Access point

The access point of a box announces `Viindoo-IoT-` followed by the hardware address of its Wi-Fi interface,
without colons (`Viindoo-IoT-b827eb1a2b3c`): each box has a name of its own, of 24 characters, and a box that
gives no such address announces `Viindoo-IoT`. hostapd runs `viin-box-access-point-name` before each start,
whoever starts it: core's `wireless_ap.sh` as the box starts and after a join that failed, or the Wi-Fi watch.
It writes the name in `/etc/hostapd/hostapd.conf` (the file `DAEMON_CONF` names), the file core's status ticket
and the box page read the name from; core gives the access point the name `IoTBox-` and the address only when
that file names exactly `IoTBox`, so the name written here is the one announced. A box updated in place
announces it from the next start of its access point. The host name of the box is not changed.

## The box user

On a box the user `pi` is root, as on the box of Odoo Community: the base system lets it run every
command through `sudo` without a password (`/etc/sudoers.d/010_pi-nopasswd`), and core relies on
that for `chpasswd`, `iwlist`, `service` and for the `mkdir`, `chown` and `timedatectl` its service
unit runs. The layer adds no sudo rules of its own, and `install_layer.sh` removes
`/etc/sudoers.d/011_viindoo-iot-box`, which an earlier version wrote. `install_on_raspios.sh`
writes `010_pi-nopasswd` itself when it has to create the user `pi`.

No list of commands could limit the user, because the box runs scripts of the tree that user owns
as root. What protects a box is the lock on the routes of the box page, the fact that no
credential is published (not the master password, not the password of the user), and the release
channel the box follows.

`install_layer.sh` sets the password field of `pi` to exactly `!`: no password logs in, over SSH
or otherwise, and no hash is kept behind the mark. Nothing else needs that password, since
`sudo` and the box service do not ask for it. The password for SSH is the one the box page shows
under "SSH password"; asking for it writes a whole new field, and when the box is paired the box
service does so itself. Running `install_layer.sh` again locks the field again.

## Roll-back guard

An update moves the box to another release and writes down, in
`/home/pi/viin-box-state.json`, the release to go back to. The box software marks a release
good once it has run normally for two minutes. Until then the guard counts the starts of the
box service; at the third it puts the box back on the release it ran, installs the system
files of that release again, and records the other one as bad: the box never moves to it
again. A restart that somebody asked for takes the place of the start it ends, and is not
counted twice. A release that neither fails nor is marked good is started again after five
minutes, so that it too comes to its third start. An update that was cut short while it moved
the box is undone at the next start and counts for nothing.

The guard uses the Python interpreter of the system and git, and nothing of the box software.
