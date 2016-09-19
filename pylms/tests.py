#!/usr/bin/env python

import logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s : %(message)s")
import pylmsserver


s = pylmsserver.LMSServer("192.168.1.3")
s.connect()

#print((s.get_players()))

p = s.get_player("piCorePlayer")
print(p)
#p.set_volume(10)
pipo = p.get_track_artist()
print(p.get_track_album())
print(p.get_track_title())

#r = s.request("songinfo 0 100 track_id:94")
#print(r)

#r = s.request("trackstat getrating 1019")
#print(r)
