#!/usr/bin/env python

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s : %(message)s")
import pylmsserver


s = pylmsserver.LMSServer("192.168.1.1")
s.connect()

print(s.get_players())

p = s.get_player("Bedroom")
print(p)
p.set_volume(10)

r = s.request("songinfo 0 100 track_id:94")
print(r)

r = s.request("trackstat getrating 1019")
print(r)
