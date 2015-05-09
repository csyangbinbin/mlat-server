# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-server: a Mode S multilateration server
# Copyright (C) 2015  Oliver Jowett <oliver@mutability.co.uk>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
The multilateration tracker: pairs up copies of the same message seen by more
than one receiver, clusters them by time, and passes them on to the solver to
derive positions.
"""

import json
import asyncio
import time
import logging
import operator
import numpy
from contextlib import closing

import modes.message
from mlat import geodesy, constants
from mlat.server import clocknorm, solver, config

glogger = logging.getLogger("mlattrack")


class MessageGroup:
    def __init__(self, first_seen, message):
        self.message = message
        self.first_seen = first_seen
        self.copies = []
        self.handle = None


class MlatTracker(object):
    def __init__(self, coordinator, pseudorange_filename=None):
        self.pending = {}
        self.coordinator = coordinator
        self.tracker = coordinator.tracker
        self.clock_tracker = coordinator.clock_tracker
        self.read_blacklist()
        self.coordinator.add_sighup_handler(self.read_blacklist)

        self.pseudorange_file = None
        self.pseudorange_filename = pseudorange_filename
        if self.pseudorange_filename:
            self.reopen_pseudoranges()
            self.coordinator.add_sighup_handler(self.reopen_pseudoranges)

    def read_blacklist(self):
        s = set()
        try:
            with closing(open('mlat-blacklist.txt', 'r')) as f:
                user = f.readline().strip()
                if user:
                    s.add(user)
        except FileNotFoundError:
            pass

        glogger.info("Read {n} blacklist entries".format(n=len(s)))
        self.blacklist = s

    def reopen_pseudoranges(self):
        if self.pseudorange_file:
            self.pseudorange_file.close()
            self.pseudorange_file = None

        self.pseudorange_file = open(self.pseudorange_filename, 'a')

    def receiver_mlat(self, receiver, timestamp, message):
        # use message as key
        group = self.pending.get(message)
        if not group:
            group = self.pending[message] = MessageGroup(time.time(), message)
            group.handle = asyncio.get_event_loop().call_later(
                config.MLAT_DELAY,
                self._resolve,
                group)

        group.copies.append((receiver, timestamp))

    def _resolve(self, group):
        del self.pending[group.message]

        # less than 3 messages -> no go
        if len(group.copies) < 3:
            return

        decoded = modes.message.decode(group.message)

        ac = self.tracker.aircraft.get(decoded.address)
        if not ac:
            return

        now = time.monotonic()

        # When we've seen a few copies of the same message, it's
        # probably correct. Update the tracker with newly seen
        # altitudes, squawks, callsigns.
        if decoded.altitude is not None:
            ac.altitude = decoded.altitude
            ac.last_altitude_time = now

        if decoded.squawk is not None:
            ac.squawk = decoded.squawk

        if decoded.callsign is not None:
            ac.callsign = decoded.callsign

        # find old result, if present
        if ac.last_result_position is None or (now - ac.last_result_time) > 120:
            last_result_position = None
            last_result_var = 1e9
            last_result_distinct = 0
            elapsed = 120
        else:
            last_result_position = ac.last_result_position
            last_result_var = ac.last_result_var
            last_result_distinct = ac.last_result_distinct
            elapsed = now - ac.last_result_time

        # find altitude
        if ac.altitude is None:
            altitude = altitude_error = None
            min_receivers = 4
        else:
            # assume 250ft accuracy at the time it is reported
            # (this bundles up both the measurement error, and
            # that we don't adjust for local pressure)
            #
            # Then degrade the accuracy over time at ~4000fpm
            altitude = ac.altitude * constants.FTOM
            altitude_error = (250 + (now - ac.last_altitude_time) * 70) * constants.FTOM
            min_receivers = 3 if (altitude_error < 1000) else 4

        # construct a map of receiver -> list of timestamps
        timestamp_map = {}
        for receiver, timestamp in group.copies:
            if receiver.user not in self.blacklist:
                timestamp_map.setdefault(receiver, []).append(timestamp)

        # check for minimum needed receivers
        if len(timestamp_map) < min_receivers:
            return

        # basic ratelimit before we do more work
        if elapsed < 15.0 and len(timestamp_map) < last_result_distinct:
            return

        if elapsed < 2.0 and len(timestamp_map) == last_result_distinct:
            return

        # normalize timestamps. This returns a list of timestamp maps;
        # within each map, the timestamp values are comparable to each other.
        components = clocknorm.normalize(clocktracker=self.clock_tracker,
                                         timestamp_map=timestamp_map)

        # cluster timestamps into clusters that are probably copies of the
        # same transmission.
        clusters = []
        for component in components:
            if len(component) >= min_receivers:  # don't bother with orphan components at all
                clusters.extend(_cluster_timestamps(component, min_receivers))

        if not clusters:
            return

        # start from the largest cluster
        result = None
        clusters.sort(key=operator.itemgetter(0))
        while clusters and not result:
            distinct, cluster = clusters.pop()

            # accept fewer receivers after 10s
            # accept the same number of receivers after MLAT_DELAY - 0.5s
            # accept more receivers immediately

            if elapsed < 10.0 and distinct < last_result_distinct:
                break

            if elapsed < (config.MLAT_DELAY - 0.5) and distinct == last_result_distinct:
                break

            cluster.sort(key=operator.itemgetter(1))  # sort by increasing timestamp (todo: just assume descending..)
            r = solver.solve(cluster, altitude, altitude_error,
                             last_result_position if last_result_position else cluster[0][0].position)
            if r:
                # estimate the error
                ecef, ecef_cov = r
                if ecef_cov is not None:
                    var_est = numpy.trace(ecef_cov)
                else:
                    # this result is suspect
                    var_est = 100e6

                if var_est > 100e6:
                    # more than 10km, too inaccurate
                    continue

                if elapsed < 2.0 and var_est > last_result_var * 1.1:
                    # less accurate than a recent position
                    continue

                #if elapsed < 10.0 and var_est > last_result_var * 2.25:
                #    # much less accurate than a recent-ish position
                #    continue

                # accept it
                result = r

        if not result:
            return

        ecef, ecef_cov = result
        ac.last_result_position = ecef
        ac.last_result_var = var_est
        ac.last_result_distinct = distinct
        ac.last_result_time = now

        ac.kalman.update(group.first_seen, cluster, altitude, altitude_error, ecef, ecef_cov, distinct)

        if min_receivers > 3:
            _, _, solved_alt = geodesy.ecef2llh(ecef)
            alt = 'missing' if (altitude is None) else '{0:.0f}'.format(altitude * constants.MTOF)
            alterr = 'missing' if (altitude is None) else '{0:.0f}'.format(altitude_error * constants.MTOF)
            glogger.info("{addr:06x} solved altitude={solved_alt:.0f}ft from alt={alt} err={alterr}".format(
                addr=decoded.address,
                solved_alt=solved_alt*constants.MTOF,
                alt=alt,
                alterr=alterr))

        for handler in self.coordinator.output_handlers:
            handler(group.first_seen, decoded.address,
                    ecef, ecef_cov,
                    [receiver for receiver, timestamp, error in cluster], distinct,
                    ac.kalman)

        if self.pseudorange_file:
            cluster_state = []
            t0 = cluster[0][1]
            for receiver, timestamp, variance in cluster:
                cluster_state.append([round(receiver.position[0], 0),
                                      round(receiver.position[1], 0),
                                      round(receiver.position[2], 0),
                                      round((timestamp-t0)*1e6, 1),
                                      round(variance*1e12, 2)])

            state = {'icao': '{a:06x}'.format(a=decoded.address),
                     'time': round(group.first_seen, 3),
                     'ecef': [round(ecef[0], 0),
                              round(ecef[1], 0),
                              round(ecef[2], 0)],
                     'ecef_cov': [round(ecef_cov[0, 0], 0),
                                  round(ecef_cov[0, 1], 0),
                                  round(ecef_cov[0, 2], 0),
                                  round(ecef_cov[1, 0], 0),
                                  round(ecef_cov[1, 1], 0),
                                  round(ecef_cov[1, 2], 0),
                                  round(ecef_cov[2, 0], 0),
                                  round(ecef_cov[2, 1], 0),
                                  round(ecef_cov[2, 2], 0)],
                     'distinct': distinct,
                     'cluster': cluster_state}

            if altitude is not None:
                state['altitude'] = round(altitude, 0)
                state['altitude_error'] = round(altitude_error, 0)

            json.dump(state, self.pseudorange_file)
            self.pseudorange_file.write('\n')


def _cluster_timestamps(component, min_receivers):
    #glogger.info("cluster these:")

    # flatten the component into a list of tuples
    flat_component = []
    for receiver, (variance, timestamps) in component.items():
        for timestamp in timestamps:
            #glogger.info("  {r} {t:.1f}us {e:.1f}us".format(r=receiver.user, t=timestamp*1e6, e=error*1e6))
            flat_component.append((receiver, timestamp, variance))

    # sort by timestamp
    flat_component.sort(key=operator.itemgetter(1))

    # do a rough clustering: groups of items with inter-item spacing of less than 2ms
    group = [flat_component[0]]
    groups = [group]
    for t in flat_component[1:]:
        if (t[1] - group[-1][1]) > 2e-3:
            group = [t]
            groups.append(group)
        else:
            group.append(t)

    # inspect each group and produce clusters
    # this is about O(n^2)-ish with group size, which
    # is why we try to break up the component into
    # smaller groups first.

    #glogger.info("{n} groups".format(n=len(groups)))

    clusters = []
    for group in groups:
        #glogger.info(" group:")
        #for r, t, e in group:
        #    glogger.info("  {r} {t:.1f}us {e:.1f}us".format(r=r.user, t=t*1e6, e=e*1e6))

        while len(group) >= 3:
            tail = group.pop()
            cluster = [tail]
            last_timestamp = tail[1]
            distinct_receivers = 1

            #glogger.info("forming cluster from group:")
            #glogger.info("  0 = {r} {t:.1f}us".format(r=head[0].user, t=head[1]*1e6))

            for i in range(len(group) - 1, -1, -1):
                receiver, timestamp, variance = group[i]
                #glogger.info("  consider {i} = {r} {t:.1f}us".format(i=i, r=receiver.user, t=timestamp*1e6))
                if (last_timestamp - timestamp) > 2e-3:
                    # Can't possibly be part of the same cluster.
                    #
                    # Note that this is a different test to the rough grouping above:
                    # that looks at the interval betwen _consecutive_ items, so a
                    # group might span a lot more than 2ms!
                    #glogger.info("   discard: >2ms out")
                    break

                # strict test for range, now.
                is_distinct = can_cluster = True
                for other_receiver, other_timestamp, other_variance in cluster:
                    if other_receiver is receiver:
                        #glogger.info("   discard: duplicate receiver")
                        can_cluster = False
                        break

                    d = receiver.distance[other_receiver]
                    if abs(other_timestamp - timestamp) > (d * 1.05 + 1e3) / constants.Cair:
                        #glogger.info("   discard: delta {dt:.1f}us > max {m:.1f}us for range {d:.1f}m".format(
                        #    dt=abs(other_timestamp - timestamp)*1e6,
                        #    m=(d * 1.05 + 1e3) / constants.Cair*1e6,
                        #    d=d))
                        can_cluster = False
                        break

                    if d < 1e3:
                        # if receivers are closer than 1km, then
                        # only count them as one receiver for the 3-receiver
                        # requirement
                        #glogger.info("   not distinct vs receiver {r}".format(r=other_receiver.user))
                        is_distinct = False

                if can_cluster:
                    #glogger.info("   accept")
                    cluster.append(group[i])
                    del group[i]
                    if is_distinct:
                        distinct_receivers += 1

            if distinct_receivers >= min_receivers:
                cluster.reverse()  # make it ascending timestamps again
                clusters.append((distinct_receivers, cluster))

    return clusters