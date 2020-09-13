# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# Name:        sfp_dnsdb
# Purpose:     SpiderFoot plug-in that resolves and gets history of domains and IPs
#
# Author:      Filip Aleksić <faleksicdev@gmail.com>
#
# Created:     2020-09-09
# Copyright:   (c) Steve Micallef
# Licence:     GPL
# -------------------------------------------------------------------------------

import json

from spiderfoot import SpiderFootEvent, SpiderFootPlugin
import time


class sfp_dnsdb(SpiderFootPlugin):
    meta = {
        "name": "DNSDB",
        "summary": "Resolve and get history of some domain and IP",
        "flags": ["apikey"],
        "useCases": ["Passive", "Footprint", "Investigate"],
        "categories": ["Passive DNS"],
        "dataSource": {
            "website": "https://www.farsightsecurity.com",
            "model": "FREE_AUTH_LIMITED",
            "references": [
                "https://docs.dnsdb.info/dnsdb-apiv2/",
                "https://www.farsightsecurity.com/get-started/"
                "https://www.farsightsecurity.com/solutions/dnsdb/",
            ],
            "apiKeyInstructions": [
                "Visit https://www.farsightsecurity.com/get-started/",
                "Select the model that best fit your needs (free or premium)",
                "Fill in the form to get API key",
                "Check your email for your API Key ",
            ],
            "favIcon": "https://www.farsightsecurity.com/favicon.ico",
            "logo": "https://www.farsightsecurity.com/assets/media/svg/farsight-logo.svg",
            "description": "Farsight Security’s DNSDB is the world’s largest "
            "database of DNS resolution and change data. Started in 2010 and "
            "updated in real-time, DNSDB provides the most comprehensive "
            "history of domains and IP addresses worldwide.",
        },
    }

    opts = {
        "api_key": "",
        "age_limit_days": 0,
        "verify": True,
        "cohostsamedomain": False,
        "maxcohost": 100,
    }

    optdescs = {
        "api_key": "DNSDB API Key.",
        "age_limit_days": "Ignore any DNSDB records older than this many days. 0 = unlimited.",
        "verify": "Verify co-hosts are valid by checking if they still resolve to the shared IP.",
        "cohostsamedomain": "Treat co-hosted sites on the same target domain as co-hosting?",
        "maxcohost": "Stop reporting co-hosted sites after this many are found, as it would likely indicate web hosting.",
    }

    results = None
    errorState = False
    cohostcount = 0

    def setup(self, sfc, userOpts=dict()):
        self.sf = sfc
        self.results = self.tempStorage()
        self.cohostcount = 0

        for opt in list(userOpts.keys()):
            self.opts[opt] = userOpts[opt]

    def watchedEvents(self):
        return ["IP_ADDRESS", "IPV6_ADDRESS", "DOMAIN_NAME"]

    # What events this module produces
    def producedEvents(self):
        return [
            "RAW_RIR_DATA",
            "INTERNET_NAME",
            "INTERNET_NAME_UNRESOLVED",
            "PROVIDER_DNS",
            "DNS_TEXT",
            "PROVIDER_MAIL",
            "IP_ADDRESS",
            "IPV6_ADDRESS",
        ]

    def query(self, endpoint, queryType, query):
        if endpoint not in ("rrset", "rdata"):
            self.sf.error(
                f"Endpoint MUST be rrset or rdata, you sent {endpoint}", False
            )
            return None

        if queryType not in ("name", "ip"):
            self.sf.error(f"Query type MUST be name or ip, you sent {queryType}", False)
            return None

        headers = {"Accept": "application/x-ndjson", "X-API-Key": self.opts["api_key"]}

        res = self.sf.fetchUrl(
            f"https://api.dnsdb.info/dnsdb/v2/lookup/{endpoint}/{queryType}/{query}",
            timeout=self.opts["_fetchtimeout"],
            useragent="SpiderFoot",
            headers=headers,
        )

        if res["code"] == "429":
            self.sf.error("You are being rate-limited by DNSDB", False)
            self.errorState = True
            return None

        if res["content"] is None:
            self.sf.info(f"No DNSDB record found for {query}")
            return None

        splittedContent = res["content"].strip().split("\n")
        if len(splittedContent) == 2:
            self.sf.info(f"No DNSDB record found for {query}")
            return None

        try:
            records = []
            for content in splittedContent:
                records.append(json.loads(content))
        except Exception as e:
            self.sf.error(f"Error processing JSON response from DNSDB: {e}", False)
            return None

        return records[1:-1]

    def emit(self, etype, data, pevent, notify=True):
        evt = SpiderFootEvent(etype, data, self.__name__, pevent)
        if notify:
            self.notifyListeners(evt)
        return evt

    def isTooOld(self, lastSeen):
        ageLimitTs = int(time.time()) - (86400 * self.opts["age_limit_days"])
        if self.opts["age_limit_days"] > 0 and lastSeen < ageLimitTs:
            self.sf.debug("Record found but too old, skipping.")
            return True
        return False

    def handleEvent(self, event):
        eventName = event.eventType
        srcModuleName = event.module
        eventData = event.data

        if self.errorState:
            return None

        self.sf.debug(f"Received event, {eventName}, from {srcModuleName}")

        if self.opts["api_key"] == "":
            self.sf.error("You enabled sfp_dnsdb but did not set an API key!", False)
            self.errorState = True
            return None

        if eventData in self.results:
            self.sf.debug(f"Skipping {eventData}, already checked.")
            return None
        self.results[eventData] = True

        responseData = set()
        coHosts = set()

        if eventName == "DOMAIN_NAME":
            rrsetRecords = self.query("rrset", "name", eventData)
            if rrsetRecords is None:
                return None

            for record in rrsetRecords:
                record = record.get("obj")
                if self.checkForStop():
                    return None

                if self.isTooOld(record.get("time_last")):
                    continue

                if record.get("rrtype") not in (
                    "A",
                    "AAAA",
                    "MX",
                    "NS",
                    "TXT",
                    "CNAME",
                ):
                    continue

                self.emit("RAW_RIR_DATA", str(record), event)
                for data in record.get("rdata"):
                    data = data.rstrip(".")
                    if data in responseData:
                        continue
                    responseData.add(data)

                    if record.get("rrtype") == "A":
                        if not self.sf.validIP(data):
                            self.sf.debug(f"Skipping invalid IP address {data}")
                            continue

                        if self.opts["verify"] and not self.sf.validateIP(
                            eventData, data
                        ):
                            self.sf.debug(
                                f"Host {eventData} no longer resolves to {data}"
                            )
                            continue

                        if not self.getTarget().matches(data):
                            coHosts.add(data)

                        evt = self.emit("IP_ADDRESS", data, event, False)

                    if record.get("rrtype") == "AAAA":

                        if not self.getTarget().matches(
                            data, includeChildren=True, includeParents=True
                        ):
                            continue

                        if not self.sf.validIP6(data):
                            self.sf.debug("Skipping invalid IPv6 address " + data)
                            continue

                        if self.opts["verify"] and not self.sf.validateIP(
                            eventData, data
                        ):
                            self.sf.debug(
                                "Host " + eventData + " no longer resolves to " + data
                            )
                            continue

                        if not self.getTarget().matches(data):
                            coHosts.add(data)

                        evt = self.emit("IPV6_ADDRESS", data, event, False)
                    elif record.get("rrtype") == "MX":
                        evt = self.emit("PROVIDER_MAIL", data, event, False)
                    elif record.get("rrtype") == "NS":
                        evt = self.emit("PROVIDER_DNS", data, event, False)
                    elif record.get("rrtype") == "TXT":
                        evt = self.emit("DNS_TEXT", data, event, False)
                    elif record.get("rrtype") == "CNAME":
                        if not self.getTarget().matches(data):
                            coHosts.add(data)

                    self.notifyListeners(evt)

            rdataRecords = self.query("rdata", "name", eventData)
            if rdataRecords is None:
                return None
            for record in rdataRecords:
                record = record.get("obj")
                if self.isTooOld(record.time_last):
                    continue

                if record.get("rrtype") not in ("NS", "CNAME"):
                    continue
                data = record.get("rrname").rstrip(".")

                if data in responseData:
                    continue
                responseData.add(data)
                if record.get("rrtype") == "NS":
                    evt = self.emit("PROVIDER_DNS", data, event, False)
                elif record.get("rrtype") == "CNAME":
                    if not self.getTarget().matches(data):
                        coHosts.add(data)

        elif eventName in ("IP_ADDRESS", "IPV6_ADDRESS"):
            rdataRecords = self.query("rdata", "ip", eventData)
            if rdataRecords is None:
                return None

            for record in rdataRecords:
                record = record.get("obj")
                if self.checkForStop():
                    return None

                if self.isTooOld(record.get("time_last")):
                    continue

                if record.get("rrtype") not in ("A", "AAAA"):
                    continue

                data = record.get("rrname").rstrip(".")

                if data in responseData:
                    continue
                responseData.add(data)

                self.emit("RAW_RIR_DATA", str(record), event)

                if self.opts["verify"] and not self.sf.resolveHost(data):
                    self.sf.debug(f"Host {data} could not be resolved")
                    evt = SpiderFootEvent(
                        "INTERNET_NAME_UNRESOLVED", data, self.__name__, event
                    )
                    self.notifyListeners(evt)
                else:
                    evt = SpiderFootEvent("INTERNET_NAME", data, self.__name__, event)
                    self.notifyListeners(evt)

                if not self.getTarget().matches(data):
                    coHosts.add(data)

        for co in coHosts:
            if eventName == "IP_ADDRESS" and (
                self.opts["verify"] and not self.sf.validateIP(co, eventData)
            ):
                self.sf.debug("Host no longer resolves to our IP.")
                continue

            if not self.opts["cohostsamedomain"]:
                if self.getTarget().matches(co, includeParents=True):
                    self.sf.debug(
                        "Skipping " + co + " because it is on the same domain."
                    )
                    continue

            if self.cohostcount < self.opts["maxcohost"]:
                self.emit("CO_HOSTED_SITE", co, self.__name__, event)
                self.cohostcount += 1


# End of sfp_dnsdb class
