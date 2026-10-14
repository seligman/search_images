/* The Trips pages: /trips lists the trips found in the pictures (trips.py:
   libraries named after a trip, and runs of days away from home), newest
   first, by year; /trip/<id> is one trip: its title, dates and the places
   it went to, a map of its drive routes, its located pictures and their
   estimated places (hollow pins), and a section for each day with the
   places of the day, a strip of the day's best pictures (a click opens
   them in the viewer) and "Open in search". The admin renames a trip,
   hides it (and shows it again), merges it with the trip before or after
   it, splits it at a day, forgets those changes ("As found") and exports
   it (the Manage page's export dialog, see trip_export.py). A trip's
   export opens on its trip's page (index.html, the page settings' trip):
   its records have no times, so the day's order and how far apart the
   pictures were taken come from the trip's days (ids in the order taken,
   gaps: one digit for each picture after the first, trips.gap_classes).

   The trips come from SITE.loadTrips (the days and the ids of their
   pictures); the pictures from SITE.loadIndex, so a picture shows as it
   does on the search page. The page shows (in its strips, its map, its
   viewer and its counts) only what the search page shows by default
   (shown): no explicit or graphic picture, and hidden ones to the admin
   only. The best pictures of a day (bestOf): never a hidden, private or
   sensitive one, nor a screenshot, document, scan, diagram or meme, nor a
   private document (an ID card); the favorites first, then the higher
   rating, then -- after the others -- a picture of a sign, menu or other
   paper, of a screen, or marked accidental (demoted), then the better
   looking (the description's aesthetic and sharpness scores, less for
   blurry, dark or overexposed ones); one picture of each unit: a stack
   of near-duplicates (dg: copies, bursts and look-alikes, see
   search_index.py) and the day's pictures that look alike (the day's
   looks, trips.look_units), joined where they meet, and each drive (dv:
   its middle clip); spread over the day (a picture taken within
   SPREAD_SECONDS of one already chosen waits until every other has had
   its turn, and one taken within SAME_MOMENT_SECONDS of it is left out);
   in the order taken. A day with none of those shows its best picture
   that is not hidden, private or sensitive (a document, say: fallback).
   A trip's cover takes one picture a day first, unless another day has
   only clearly worse ones (by more than half a point of the aesthetic
   score: COVER_SLACK), and leaves 360 photos and wide panoramas (cropped
   hard in its cells) to the end.

   "Open in search" opens exactly the pictures the page shows (of the trip,
   a day, or its other pictures: SITE.idList, with a "Trip: Name" chip),
   else (too many for an address) a search close to them. The viewer gets a
   history entry of its own: Back closes it. Moving between the list and a
   trip within the page puts the focus on the new page's title.

   The trip's story (trip_stories.py: a paragraph a day written by a
   language model from the trip's facts) shows in the days' sections, its
   links to pictures ([#id]) as small pictures that open the viewer; the
   server's comes from SITE.loadTripStory, an export's is on its trip's days
   (story). The admin writes it (the trip_stories job, after a look at what
   it takes: /trips/stories/plan), edits, hides or deletes it, and writes
   the stories of every trip from the list.

   TRIPS holds the parts without a page (dates in words, the best pictures,
   the searches of a trip and of its days, the story's parts and words), run
   by the tests under Node (tests/js/trips_test.js). Pages are built with DOM
   calls and textContent only (never innerHTML).
*/
var TRIPS = (function () {
    "use strict";

    var hasDocument = typeof document !== "undefined" && document !== null;

    var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December"];
    var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
    var DOT = " \u00b7 ";
    // Kinds of pictures that are never among a trip's best (search_index's ty, vt), and
    // document types that are private (export.py's PRIVATE_DOCUMENT_TYPES).
    var NOT_BEST_TYPES = ["screenshot", "document", "scan", "diagram", "meme"];
    var PRIVATE_DOCUMENTS = ["card_or_id"];
    // Quality flags that put a picture after the others (unless it is a favorite or
    // rated), and those that count against it.
    var DEMOTING_FLAGS = ["accidental", "screen_photo"];
    var FLAW_COST = {blurry: 15, dark: 6, overexposed: 6};
    // Sensitive levels the search page leaves out by default (search-core.js).
    var NOT_SHOWN_SENSITIVE = ["explicit", "graphic"];
    // Pixels count among pictures of the same rank up to this many: a 360 photo or a
    // stitched panorama is not better for being larger.
    var PIXELS_CAP = 12000000;
    // On a cover, these are left to the end (cropped hard in its cells).
    var WIDE_ASPECT = 2.2;
    // A trip's cover takes one picture a day first, unless the best of another day
    // is more than this much worse (quality(): 10 is a point of the aesthetic score).
    var COVER_SLACK = 5;
    // A picture taken this close to one already chosen waits its turn; one
    // taken this close (seconds) is left out (the same moment twice).
    var SPREAD_SECONDS = 120;
    var SAME_MOMENT_SECONDS = 20;
    // How many best pictures a trip's card shows on the list.
    var COVER_COUNT = 4;
    // A trip of more days than this gets a row of links to them under its places.
    var DAY_LINKS_FROM = 5;

    function isText(value) {
        return typeof value === "string" && value !== "";
    }

    /* ---------- Dates in words --------------------------------------------- */

    // {year, month (1-12), day, weekday (0 Sunday)} of "YYYY-MM-DD", or null.
    function parseDay(text) {
        var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || ""));
        if (!match) {
            return null;
        }
        var date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
        if (date.getUTCMonth() !== Number(match[2]) - 1) {
            return null;
        }
        return {year: Number(match[1]), month: Number(match[2]), day: Number(match[3]),
                weekday: date.getUTCDay(), time: date.getTime()};
    }

    // "Saturday, 9 November 2024".
    function dayWords(text) {
        var day = parseDay(text);
        if (!day) {
            return "";
        }
        return DAYS[day.weekday] + ", " + day.day + " " + MONTHS[day.month - 1] + " " + day.year;
    }

    // "9-15 November 2024", "30 October - 2 November 2024", "30 December 2023 - 2 January
    // 2024", or "9 November 2024" (trips.span_words).
    function spanWords(start, end) {
        var first = parseDay(start);
        var last = parseDay(end);
        if (!first || !last) {
            return "";
        }
        function whole(day) {
            return day.day + " " + MONTHS[day.month - 1] + " " + day.year;
        }
        if (first.time === last.time) {
            return whole(first);
        }
        if (first.year !== last.year) {
            return whole(first) + " \u2013 " + whole(last);
        }
        if (first.month !== last.month) {
            return first.day + " " + MONTHS[first.month - 1] + " \u2013 " + whole(last);
        }
        return first.day + "\u2013" + whole(last);
    }

    // Days from start to end, both counted (1 for one day; 0 when unknown).
    function dayCount(start, end) {
        var first = parseDay(start);
        var last = parseDay(end);
        if (!first || !last) {
            return 0;
        }
        return Math.round((last.time - first.time) / 86400000) + 1;
    }

    function plural(count, word, many) {
        return Number(count).toLocaleString("en-US") + " " +
            (count === 1 ? word : (many || word + "s"));
    }

    // "14:32" of a record's time taken, or "".
    function clock(rec) {
        var match = rec && /T(\d{2}):(\d{2})/.exec(rec.ts || "");
        return match ? match[1] + ":" + match[2] : "";
    }

    /* ---------- The best pictures ------------------------------------------ */

    // True when the page shows a picture at all (its strips, map, viewer and
    // counts): as the search page does by default, never an explicit or graphic
    // one, and a hidden one only to the admin.
    function shown(rec, admin) {
        return Boolean(rec) && typeof rec === "object" && !(rec.h && !admin) &&
            NOT_SHOWN_SENSITIVE.indexOf(rec.se) < 0;
    }

    // True when a picture may be shown at all as a day's picture: not hidden,
    // private or sensitive.
    function presentable(rec) {
        return Boolean(rec) && typeof rec === "object" && !rec.h && !rec.pr &&
            !(isText(rec.se) && rec.se !== "none");
    }

    // True when a picture may be among the best: see the notes at the top.
    function eligible(rec) {
        if (!presentable(rec)) {
            return false;
        }
        if (NOT_BEST_TYPES.indexOf(rec.ty) >= 0 || NOT_BEST_TYPES.indexOf(rec.vt) >= 0) {
            return false;
        }
        return PRIVATE_DOCUMENTS.indexOf(rec.dt) < 0;
    }

    // True when a picture comes after the others of its rank: a sign, a menu or
    // another paper (dt), a picture of a screen or one marked accidental -- unless
    // it is a favorite or rated.
    function demoted(rec) {
        if (rec.fav || rec.rt) {
            return false;
        }
        return (isText(rec.dt) && rec.dt !== "none") ||
            (Array.isArray(rec.qf) ? rec.qf : []).some(function (flag) {
                return DEMOTING_FLAGS.indexOf(flag) >= 0;
            });
    }

    // True for a 360-degree picture or one much wider than high.
    function isWide(rec) {
        return rec.p === "360" || (rec.w > 0 && rec.hgt > 0 && rec.w / rec.hgt > WIDE_ASPECT);
    }

    // The description's scores as one number (aesthetic first, then sharpness), less
    // what its quality flags cost.
    function quality(rec) {
        var value = (typeof rec.q === "number" ? rec.q : 0) * 10 +
            (typeof rec.sh === "number" ? rec.sh : 0);
        (Array.isArray(rec.qf) ? rec.qf : []).forEach(function (flag) {
            value -= FLAW_COST[flag] || 0;
        });
        return value;
    }

    // Moments (seconds) of the pictures of an export, which has no times: {id: its
    // day's first second, plus 1, SAME_MOMENT_SECONDS or more than SPREAD_SECONDS
    // after the picture before it, by the day's gaps} (useMoments).
    var pseudoMoments = {};

    // Seconds since 1970 of a record's time taken (as if UTC), or in an export its
    // pseudo moment; null when not known.
    function moment(rec) {
        if (rec && !rec.ts && pseudoMoments[rec.i] !== undefined) {
            return pseudoMoments[rec.i];
        }
        var text = rec && rec.ts;
        var match = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(text || "");
        if (!match) {
            return null;
        }
        return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]),
                        Number(match[4] || 0), Number(match[5] || 0),
                        Number(match[6] || 0)) / 1000;
    }

    // {id: pseudo moment} of a day of a trip from its gaps ({} without them).
    function dayMoments(day) {
        var result = {};
        var first = parseDay(day && day.date);
        var gaps = day && typeof day.gaps === "string" ? day.gaps : null;
        if (!first || gaps === null || !Array.isArray(day.ids)) {
            return result;
        }
        var at = first.time / 1000;
        day.ids.forEach(function (id, index) {
            if (index) {
                var gap = gaps.charAt(index - 1);
                at += gap === "0" ? 1 : gap === "1" ? SAME_MOMENT_SECONDS : SPREAD_SECONDS + 1;
            }
            result[id] = at;
        });
        return result;
    }

    // Use the pseudo moments of the days of some trips (an export's: see dayMoments).
    function useMoments(trips) {
        pseudoMoments = {};
        (trips || []).forEach(function (trip) {
            (trip.days || []).forEach(function (day) {
                var found = dayMoments(day);
                Object.keys(found).forEach(function (id) {
                    pseudoMoments[id] = found[id];
                });
            });
        });
    }

    function pixels(rec) {
        return Math.min(PIXELS_CAP, (rec.w || 0) * (rec.hgt || 0));
    }

    // Negative when a ranks before b: favorites, rating, not demoted, looks, in an
    // album, pixels (up to PIXELS_CAP).
    function compareRank(a, b) {
        return (b.fav ? 1 : 0) - (a.fav ? 1 : 0) ||
            (b.rt || 0) - (a.rt || 0) ||
            (demoted(a) ? 1 : 0) - (demoted(b) ? 1 : 0) ||
            quality(b) - quality(a) ||
            ((b.al && b.al.length) ? 1 : 0) - ((a.al && a.al.length) ? 1 : 0) ||
            pixels(b) - pixels(a) ||
            (a.i < b.i ? -1 : a.i > b.i ? 1 : 0);
    }

    // A cover's order: compareRank's, with 360 photos and wide panoramas after the
    // others of their favorite and rating.
    function compareCover(a, b) {
        return (b.fav ? 1 : 0) - (a.fav ? 1 : 0) ||
            (b.rt || 0) - (a.rt || 0) ||
            (isWide(a) ? 1 : 0) - (isWide(b) ? 1 : 0) ||
            compareRank(a, b);
    }

    // The part of the rank quality() does not say: favorite, rating, demoted.
    function tier(rec) {
        return (rec.fav ? 1000 : 0) + (rec.rt || 0) * 10 + (demoted(rec) ? 0 : 1);
    }

    // Earlier first (undated last), then by id.
    function compareTime(a, b) {
        var x = moment(a);
        var y = moment(b);
        if (x !== y) {
            if (x === null) {
                return 1;
            }
            if (y === null) {
                return -1;
            }
            return x - y;
        }
        return a.i < b.i ? -1 : a.i > b.i ? 1 : 0;
    }

    /* {id: the key of its unit} of the pictures of ids (records by get(id)): a
       drive (dv) is one unit; a stack (dg) and a look unit (looks: [[ids]],
       trips.look_units) are one too, joined with the other units of the
       pictures they hold; any other picture is a unit of its own. */
    function unitKeys(ids, get, looks) {
        var parent = {};
        function find(key) {
            while (parent[key] !== key) {
                parent[key] = parent[parent[key]];
                key = parent[key];
            }
            return key;
        }
        function join(first, second) {
            if (parent[first] === undefined) {
                parent[first] = first;
            }
            if (parent[second] === undefined) {
                parent[second] = second;
            }
            first = find(first);
            second = find(second);
            if (first !== second) {
                parent[second] = first;
            }
        }
        var own = {};
        (ids || []).forEach(function (id) {
            var rec = get(id);
            if (!rec) {
                return;
            }
            if (rec.dv !== undefined && rec.dv !== null) {
                own[id] = "dv:" + rec.dv;
                parent[own[id]] = own[id];
                return;
            }
            own[id] = "i:" + rec.i;
            parent[own[id]] = own[id];
            if (rec.dg !== undefined && rec.dg !== null) {
                join("dg:" + rec.dg, own[id]);
            }
        });
        (looks || []).forEach(function (unit, number) {
            (Array.isArray(unit) ? unit : []).forEach(function (id) {
                if (own[id] && own[id].charAt(0) === "i") {
                    join("lu:" + number, own[id]);
                }
            });
        });
        var result = {};
        Object.keys(own).forEach(function (id) {
            result[id] = find(own[id]);
        });
        return result;
    }

    /* The best pictures of ids (records by get(id)): at most count ids, in the
       order taken (see the notes at the top). options.looks: the look units of
       the pictures ([[ids]]); options.perDay: at first one a day (a trip's cover,
       see the notes). */
    function bestOf(ids, get, count, options) {
        options = options || {};
        var keys = unitKeys(ids, get, options.looks);
        var units = {};
        var order = [];
        (ids || []).forEach(function (id) {
            var rec = get(id);
            if (!eligible(rec)) {
                return;
            }
            var key = keys[id];
            if (!units[key]) {
                units[key] = [];
                order.push(key);
            }
            units[key].push(rec);
        });
        var compare = options.perDay ? compareCover : compareRank;
        var ranked = order.map(function (key) {
            var members = units[key];
            if (key.indexOf("dv:") === 0) {
                // A drive shows its middle clip (as its tile on the search page
                // does): its first is often where it was parked.
                members.sort(compareTime);
                return members[(members.length - 1) >> 1];
            }
            return members.sort(compare)[0];
        }).sort(compare);
        var chosen = [];
        var days = {};
        function near(rec, seconds) {
            var at = moment(rec);
            return at !== null && chosen.some(function (other) {
                var then = moment(other);
                return then !== null && Math.abs(then - at) < seconds;
            });
        }
        function dayDone(rec) {
            return isText(rec.dk) && days[rec.dk] === true;
        }
        // (On a cover: a picture of a day shown already gives way to the best one of
        // another day, unless that one is clearly worse.)
        function givesWay(rec, at) {
            for (var k = at + 1; k < ranked.length; k++) {
                var other = ranked[k];
                if (!dayDone(other) && !near(other, SPREAD_SECONDS)) {
                    return tier(other) === tier(rec) &&
                        quality(other) >= quality(rec) - COVER_SLACK;
                }
            }
            return false;
        }
        // The best of those left, one at a time: not close to one chosen (nor, on a
        // cover, of a day shown already when another day's is about as good).
        function next() {
            for (var at = 0; at < ranked.length; at++) {
                var rec = ranked[at];
                if (!near(rec, SPREAD_SECONDS) &&
                        !(options.perDay && dayDone(rec) && givesWay(rec, at))) {
                    return at;
                }
            }
            return -1;
        }
        while (chosen.length < count) {
            var at = next();
            if (at < 0) {
                break;
            }
            var rec = ranked.splice(at, 1)[0];
            chosen.push(rec);
            if (isText(rec.dk)) {
                days[rec.dk] = true;
            }
        }
        // Then those close to one chosen (best first), but not of the same moment.
        ranked.forEach(function (rec) {
            if (chosen.length < count && !near(rec, SAME_MOMENT_SECONDS)) {
                chosen.push(rec);
            }
        });
        return chosen.sort(compareTime).map(function (rec) {
            return rec.i;
        });
    }

    /* The picture a day shows when none of its pictures may be among its best
       (bestOf found none): its best that is not hidden, private or sensitive (a
       document, a screenshot), or null. */
    function fallback(ids, get) {
        var found = (ids || []).map(get).filter(presentable).sort(compareRank);
        return found.length ? found[0].i : null;
    }

    /* Why a day shows no picture at all (fallback found none): "Nothing to show
       here: its pictures are private or sensitive." -- naming only the reasons
       there are among its pictures (hidden ones: the admin's page only). */
    var REASON_ORDER = ["hidden", "private", "sensitive"];

    function noneWords(ids, get) {
        var reasons = [];
        var count = 0;
        function add(word) {
            if (reasons.indexOf(word) < 0) {
                reasons.push(word);
            }
        }
        (ids || []).forEach(function (id) {
            var rec = get(id);
            if (!rec) {
                return;
            }
            count += 1;
            if (rec.h) {
                add("hidden");
            }
            if (rec.pr) {
                add("private");
            }
            if (isText(rec.se) && rec.se !== "none") {
                add("sensitive");
            }
        });
        if (!reasons.length) {
            return "Nothing to show here.";
        }
        reasons.sort(function (a, b) {
            return REASON_ORDER.indexOf(a) - REASON_ORDER.indexOf(b);
        });
        var last = reasons.pop();
        return "Nothing to show here: " + (count === 1 ? "its picture is "
            : "its pictures are ") +
            (reasons.length ? reasons.join(", ") + " or " + last : last) + ".";
    }

    /* What a list of pictures holds: {pictures (not videos), videos (videos not in
       a drive), drives (how many), panoramas (360-degree pictures and videos)}. */
    function facts(ids, get) {
        var result = {pictures: 0, videos: 0, drives: 0, panoramas: 0};
        var drives = {};
        (ids || []).forEach(function (id) {
            var rec = get(id);
            if (!rec) {
                return;
            }
            if (rec.p === "360") {
                result.panoramas += 1;
            }
            if (!rec.mv) {
                result.pictures += 1;
            } else if (rec.dv !== undefined && rec.dv !== null) {
                if (!drives[rec.dv]) {
                    drives[rec.dv] = true;
                    result.drives += 1;
                }
            } else {
                result.videos += 1;
            }
        });
        return result;
    }

    // "48 pictures \u00b7 3 videos \u00b7 1 drive \u00b7 2 in 360\u00b0".
    function factsWords(found) {
        var parts = [];
        if (found.pictures) {
            parts.push(plural(found.pictures, "picture"));
        }
        if (found.videos) {
            parts.push(plural(found.videos, "video"));
        }
        if (found.drives) {
            parts.push(plural(found.drives, "drive"));
        }
        if (found.panoramas) {
            parts.push(Number(found.panoramas).toLocaleString("en-US") + " in 360\u00b0");
        }
        return parts.join(DOT);
    }

    /* ---------- Searches of a trip and its days ---------------------------- */

    // The libraries of some pictures (records' l), in the order of libraries.
    function librariesOf(ids, get, libraries) {
        var seen = {};
        (ids || []).forEach(function (id) {
            var rec = get(id);
            if (rec && isText(rec.l)) {
                seen[rec.l] = true;
            }
        });
        var result = (libraries || []).filter(function (id) {
            return seen[id];
        });
        Object.keys(seen).sort().forEach(function (id) {
            if (result.indexOf(id) < 0) {
                result.push(id);
            }
        });
        return result;
    }

    /* The search (SITE.searchState) of some pictures of a trip, oldest first:
       exactly those (ids) when encode(ids) gives the text of their list
       (SITE.idList.encode's, "" when it is too long for an address), with
       label as what the search page calls them ("Trip: Name"); else
       fallback(). */
    function idsSearch(ids, label, encode, fallback) {
        var text = encode && ids && ids.length ? encode(ids) : "";
        if (text) {
            return {ids: text, pick: label, sort: "oldest"};
        }
        return fallback();
    }

    /* What the search of a trip's pictures falls back to (and the big map shows:
       it takes no list of pictures): a trip library alone (its own pictures,
       whatever their dates), else its libraries on its dates. */
    function mapSearch(trip) {
        var libraries = (trip.libraries || []).slice();
        if (trip.kind === "library" && libraries.length) {
            return {filters: {library: libraries.slice(0, 1)}, sort: "oldest"};
        }
        var state = {filters: {library: libraries}, sort: "oldest"};
        if (trip.start && trip.end) {
            state.from = trip.start;
            state.to = trip.end;
        }
        return state;
    }

    // The search of a trip's pictures (ids: those the page shows; idsSearch).
    function tripSearch(trip, ids, encode) {
        return idsSearch(ids, "Trip: " + trip.title, encode, function () {
            return mapSearch(trip);
        });
    }

    // The search of a day of a trip (ids: those the page shows of it), else the
    // libraries of its pictures on that day.
    function daySearch(trip, day, number, ids, get, encode) {
        return idsSearch(ids, "Trip: " + trip.title + ", day " + number, encode, function () {
            return {filters: {library: librariesOf(day.ids, get, trip.libraries)},
                    from: day.date, to: day.date, sort: "oldest"};
        });
    }

    // The search of a trip's other pictures (no date within its own), else the
    // libraries they are in.
    function otherSearch(trip, ids, get, encode) {
        return idsSearch(ids, "Trip: " + trip.title + ", other pictures", encode, function () {
            return {filters: {library: librariesOf(trip.other, get, trip.libraries)},
                    sort: "oldest"};
        });
    }

    // [{id, lat, lon, e}] of the located points (SITE.loadLocations') of a trip.
    function tripPoints(trip, locations) {
        var wanted = {};
        (trip.days || []).forEach(function (day) {
            day.ids.forEach(function (id) {
                wanted[id] = true;
            });
        });
        (trip.other || []).forEach(function (id) {
            wanted[id] = true;
        });
        return (locations || []).filter(function (point) {
            return point && wanted[point.id] === true;
        });
    }

    /* The points a trip's map is fitted to: all of them, but a few (at most
       FAR_SHARE of them) much further from the middle (the median) than the
       others -- more than FAR_FACTOR times as far as three in four of them,
       and more than FAR_KM -- are left out (a picture whose date or position
       is wrong): they are on the map, and zooming out shows them. More of
       them are a part of the trip (a flight home by way of another city). */
    var FAR_FACTOR = 6;
    var FAR_KM = 150;
    var FAR_SHARE = 0.05;

    function fitPoints(points) {
        points = (points || []).filter(function (point) {
            return point && typeof point.lat === "number" && typeof point.lon === "number";
        });
        if (points.length < 4) {
            return points;
        }
        function median(values) {
            values = values.slice().sort(function (a, b) {
                return a - b;
            });
            return values[values.length >> 1];
        }
        var lat = median(points.map(function (point) {
            return point.lat;
        }));
        var lon = median(points.map(function (point) {
            return point.lon;
        }));
        var scale = Math.cos(lat * Math.PI / 180);
        var far = points.map(function (point) {
            var dx = ((point.lon - lon + 540) % 360 - 180) * scale;
            var dy = point.lat - lat;
            return Math.sqrt(dx * dx + dy * dy) * 111.2;
        });
        var sorted = far.slice().sort(function (a, b) {
            return a - b;
        });
        var limit = Math.max(sorted[Math.floor(sorted.length * 0.75)] * FAR_FACTOR, FAR_KM);
        var kept = points.filter(function (point, index) {
            return far[index] <= limit;
        });
        return points.length - kept.length <= Math.floor(points.length * FAR_SHARE)
            ? kept : points;
    }

    // The look units of all of a trip's days (and its other pictures), together.
    function tripLooks(trip) {
        var units = [];
        (trip.days || []).forEach(function (day) {
            units.push.apply(units, Array.isArray(day.looks) ? day.looks : []);
        });
        units.push.apply(units, Array.isArray(trip.other_looks) ? trip.other_looks : []);
        return units;
    }

    // Every id of a trip (its days', then its other pictures').
    function tripIds(trip) {
        var ids = [];
        (trip.days || []).forEach(function (day) {
            ids.push.apply(ids, day.ids);
        });
        ids.push.apply(ids, trip.other || []);
        return ids;
    }

    /* ---------- The trip's story (trip_stories.py) ------------------------- */

    // The parts of a paragraph of a story: its text and its links to pictures
    // ([#id]: the id of a picture, or in an export its export id), in order:
    // [{text: "..."}, {id: "123"}, ...].
    function storyParts(text) {
        var parts = [];
        var pattern = /\[#([0-9A-Za-z]{1,40})\]/g;
        var at = 0;
        var match;
        text = String(text || "");
        while ((match = pattern.exec(text)) !== null) {
            if (match.index > at) {
                parts.push({text: text.slice(at, match.index)});
            }
            parts.push({id: match[1]});
            at = pattern.lastIndex;
        }
        if (at < text.length) {
            parts.push({text: text.slice(at)});
        }
        return parts;
    }

    // {date: paragraph} of a trip's story: the server's (loaded: the answer of
    // /api/trips/<id>/story), or an export's (on its trip's days: story).
    function storyDays(trip, loaded) {
        var result = {};
        if (loaded && Array.isArray(loaded.days)) {
            loaded.days.forEach(function (day) {
                if (day && isText(day.date) && isText(day.text)) {
                    result[day.date] = day.text;
                }
            });
            return result;
        }
        ((trip && trip.days) || []).forEach(function (day) {
            if (day && isText(day.date) && isText(day.story)) {
                result[day.date] = day.story;
            }
        });
        return result;
    }

    function money(value) {
        return "$" + (value >= 1 ? value.toFixed(2) : value >= 0.01 ? value.toFixed(3)
            : value.toFixed(4));
    }

    // What a trip_stories job would do, in words, from its plan (the admin's
    // /trips/stories/plan): ["2 trips: Lakes, Island.", "With big (model m), on this
    // computer: about 3,400 tokens of text; no picture is sent.", ...].
    function planWords(plan) {
        var lines = [];
        var chosen = (plan && plan.trips) || [];
        if (chosen.length) {
            lines.push(plural(chosen.length, "story", "stories") + " to write: " +
                       chosen.slice(0, 5).map(function (item) {
                           return item.title;
                       }).join(", ") + (chosen.length > 5 ? ", \u2026" : "") + ".");
        } else {
            lines.push("No story to write.");
        }
        ((plan && plan.endpoints) || []).forEach(function (entry) {
            var words = "With " + entry.name + (entry.model ? " (model " + entry.model + ")" : "");
            if (entry.local) {
                words += ", on this computer: about " + plural(entry.tokens_in, "token") +
                    " of text to read and " + Number(entry.tokens_out).toLocaleString("en-US") +
                    " to write.";
            } else {
                words += ": about " + plural(entry.tokens_in, "token") + " of text " +
                    "(without the names of people) sent to " +
                    (entry.host || "the service") + " and " +
                    Number(entry.tokens_out).toLocaleString("en-US") + " written back, " +
                    (typeof entry.cost === "number" ? "about " + money(entry.cost) + "."
                        : "at a cost not known (the endpoint has no prices).");
            }
            lines.push(words);
        });
        ((plan && plan.problems) || []).forEach(function (item) {
            lines.push(item.title + ": " + item.problem);
        });
        var skipped = (plan && plan.skipped) || {};
        var kept = [];
        if (skipped.current) {
            kept.push(plural(skipped.current, "story", "stories") + " up to date");
        }
        if (skipped.edited) {
            kept.push(plural(skipped.edited, "story", "stories") + " you edited");
        }
        if (skipped.empty) {
            kept.push(plural(skipped.empty, "trip") + " with nothing to tell");
        }
        if (kept.length) {
            lines.push("Left as they are: " + kept.join(", ") + ".");
        }
        if (chosen.length) {
            lines.push("Only text goes to the model (places, dates, the pictures' titles, " +
                       "descriptions and tags, and the names of people only to a model on " +
                       "this computer or your network): never a picture, nothing hidden and " +
                       "nothing near home.");
        }
        return lines;
    }

    // How a trip's story came about, in words (the admin's /api/trips/<id>/story):
    // "Written 13 October 2026 with big-model on this computer \u00b7 edited by you".
    function storyStateWords(story) {
        if (!story) {
            return "";
        }
        if (story.state === "missing") {
            return "No story yet.";
        }
        if (story.state === "empty") {
            return "Nothing to write a story from: the trip's pictures are hidden, private " +
                "or near home.";
        }
        var bits = [];
        var made = typeof story.made === "number" && story.made > 0 ? new Date(story.made * 1000)
            : null;
        var first = made ? "Written " + made.getDate() + " " + MONTHS[made.getMonth()] + " " +
            made.getFullYear() : "Written";
        if (story.model) {
            first += " with " + story.model + (story.local ? " on this computer" : "");
        } else if (!made) {
            first = "Written by you";
        }
        bits.push(first);
        if (story.edited && (story.model || made)) {
            bits.push("edited by you");
        }
        if (story.hidden) {
            bits.push("hidden: only you see it");
        }
        if (story.state === "stale") {
            bits.push("out of date: " + (story.reasons || []).join("; "));
        }
        return bits.join(DOT);
    }

    /* ---------- The trip's map ---------------------------------------------- */

    /* A map of a trip: its drive routes (lines), its located pictures (pins;
       the clips on a line have none) and estimated places (hollow), fitted to
       them. Dragged (or swiped sideways) it moves; the buttons, Ctrl and the
       wheel, a double click and two fingers zoom; the keys: arrows move it,
       + and - zoom, 0 fits it again, Enter opens the pin nearest the middle.
       A click on a pin opens its pictures (onPin(ids)), on a line the clip
       there (onRoute(hit)). Its ground is the map page's (STREETS.Basemap:
       the world image, and the street maps where there are any, credited
       in a corner while they show; it zooms and fits as deep as they go, as
       the map page does). options: {points, lines (ROUTES.lines'), onPin,
       onRoute}. */
    function TripMap(options) {
        var el = SHELL.el;
        var self = this;
        this.options = options;
        this.TILE = SITE.tileSize();
        var tilesMax = SITE.tilesMax();
        var streets = SITE.streets();
        this.tileZoom = tilesMax === null ? 3 : Math.max(0, tilesMax);
        this.baseMaxZoom = Math.max(this.tileZoom + 4, 10);
        this.maxZoom = Math.max(this.baseMaxZoom,
                                streets ? MAP.FIT_STREETS + STREETS.OVERZOOM : 0);
        this.fitZoom = streets ? MAP.FIT_STREETS : this.tileZoom;
        this.streetsKnown = !streets;
        this.view = {lat: 0, lon: 0, zoom: 1, w: 0, h: 0};
        this.groups = {zoom: -1, list: []};
        this.pins = [];
        this.drawn = [];
        this.pending = false;
        var onRoutes = {};
        (options.lines || []).forEach(function (line) {
            line.forEach(function (point) {
                onRoutes[point.clip] = true;
            });
        });
        this.pinPoints = (options.points || []).filter(function (point) {
            return !onRoutes[point.id];
        });
        this.canvas = el("canvas", {className: "trip-map-canvas", tabIndex: 0,
                                    attrs: {role: "application", "aria-roledescription": "map",
                                            "aria-label": "Map of the trip. Arrow keys move " +
                                            "it, plus and minus zoom, 0 shows all of it, Enter " +
                                            "opens the pictures of the pin nearest the middle."}});
        this.tiles = new STREETS.Basemap({tileUrl: SITE.tileUrl, tileSize: this.TILE,
                                          tileZoom: this.tileZoom, ocean: "#1a2c3d",
                                          streets: streets, onLoad: function () {
                                              self.streetsChanged();
                                              self.schedule();
                                          }});
        this.credit = STREETS.creditNode("p", "map-streets-credit trip-map-credit", streets);
        function zoomButton(text, label, step) {
            return el("button", {type: "button", className: "trip-map-zoom", text: text,
                                 title: label, attrs: {"aria-label": label},
                                 on: {click: function () {
                                     if (step) {
                                         self.zoomAt(self.view.w / 2, self.view.h / 2,
                                                     self.view.zoom + step);
                                     } else {
                                         self.fit();
                                     }
                                 }}});
        }
        this.tools = el("div", {className: "trip-map-tools"}, [
            zoomButton("+", "Zoom in (+)", 1),
            zoomButton("\u2212", "Zoom out (-)", -1),
            zoomButton("\u2922", "Show the whole trip (0)", 0)
        ]);
        this.node = el("div", {className: "trip-map"}, [this.canvas, this.tools, this.credit]);
        this.listen();
        // (Another map of the page may have loaded the street maps' index already.)
        this.streetsChanged();
        if (typeof ResizeObserver === "function") {
            new ResizeObserver(function () {
                self.resize();
            }).observe(this.canvas);
        }
        window.addEventListener("resize", function () {
            self.resize();
        });
    }

    TripMap.prototype.width = function () {
        return MAP.worldWidth(this.TILE, this.view.zoom);
    };

    TripMap.prototype.corner = function () {
        var w = this.width();
        var centre = MAP.project(this.view.lat, this.view.lon, w);
        return {left: centre.x - this.view.w / 2, top: centre.y - this.view.h / 2, w: w};
    };

    TripMap.prototype.allPoints = function () {
        var list = fitPoints(this.options.points);
        (this.options.lines || []).forEach(function (line) {
            line.forEach(function (point) {
                list.push({lat: point.lat, lon: point.lon});
            });
        });
        return list;
    };

    /* The street maps' index is in (or could not be loaded): zoom as deep as
       they go, and fit again unless the map was moved since. */
    TripMap.prototype.streetsChanged = function () {
        if (this.streetsKnown) {
            return;
        }
        var deepest = this.tiles.maxZoom();
        if (deepest !== null && !this.tiles.ready()) {
            return;
        }
        this.streetsKnown = true;
        this.maxZoom = Math.max(this.baseMaxZoom, deepest === null ? 0 : deepest);
        this.fitZoom = deepest === null ? this.tileZoom
            : Math.max(this.tileZoom, this.tiles.fitZoom());
        if (this.fitted && this.view.w) {
            this.fit();
        } else if (this.view.zoom > this.maxZoom) {
            this.set(this.view.lat, this.view.lon, this.maxZoom);
        }
    };

    /* How far in a fit goes: as far as the street maps have tiles at some of
       the trip's places (see the map page's fitLimit). */
    TripMap.prototype.fitLimit = function (points) {
        var limit = Math.min(this.maxZoom, this.fitZoom);
        if (!this.tiles.ready()) {
            return limit;
        }
        var deepest = -1;
        for (var k = 0; k < points.length && deepest < limit; k++) {
            var z = this.tiles.coveredZoom(points[k].lat, points[k].lon);
            if (z !== null && z > deepest) {
                deepest = z;
            }
        }
        return deepest >= 0 ? Math.max(this.tileZoom, Math.min(limit, deepest)) : this.tileZoom;
    };

    TripMap.prototype.fit = function () {
        var points = this.allPoints();
        var found = MAP.fitView(points, this.view.w, this.view.h, this.TILE, 0,
                                this.fitLimit(points), 36);
        if (found) {
            this.set(found.lat, found.lon, found.zoom);
        } else {
            this.set(20, 0, 1);
        }
        this.fitted = true;
    };

    TripMap.prototype.set = function (lat, lon, zoom) {
        var view = this.view;
        view.zoom = Math.max(0, Math.min(this.maxZoom, Math.round(zoom)));
        var w = this.width();
        var y = MAP.project(lat, 0, w).y;
        var top = view.h / 2;
        var bottom = w - view.h / 2;
        y = bottom < top ? w / 2 : Math.max(top, Math.min(bottom, y));
        view.lat = MAP.unproject(0, y, w).lat;
        view.lon = MAP.normalizeLon(lon);
        this.schedule();
    };

    TripMap.prototype.panBy = function (dx, dy) {
        var w = this.width();
        var centre = MAP.project(this.view.lat, this.view.lon, w);
        var moved = MAP.unproject(centre.x - dx, centre.y - dy, w);
        this.set(moved.lat, moved.lon, this.view.zoom);
    };

    TripMap.prototype.zoomAt = function (sx, sy, zoom) {
        zoom = Math.max(0, Math.min(this.maxZoom, Math.round(zoom)));
        if (zoom === this.view.zoom) {
            return;
        }
        var at = this.corner();
        var anchor = MAP.unproject(at.left + sx, at.top + sy, at.w);
        var w = MAP.worldWidth(this.TILE, zoom);
        var spot = MAP.project(anchor.lat, anchor.lon, w);
        var centre = MAP.unproject(spot.x + this.view.w / 2 - sx, spot.y + this.view.h / 2 - sy, w);
        this.set(centre.lat, centre.lon, zoom);
    };

    TripMap.prototype.resize = function () {
        var w = this.canvas.clientWidth;
        var h = this.canvas.clientHeight;
        if (!w || !h) {
            return;
        }
        var ratio = window.devicePixelRatio || 1;
        this.canvas.width = Math.round(w * ratio);
        this.canvas.height = Math.round(h * ratio);
        this.ratio = ratio;
        var first = !this.view.w;
        this.view.w = w;
        this.view.h = h;
        if (first || this.fitted) {
            this.fit();
            this.fitted = true;
        }
        this.schedule();
    };

    TripMap.prototype.schedule = function () {
        var self = this;
        if (this.pending) {
            return;
        }
        this.pending = true;
        window.requestAnimationFrame(function () {
            self.pending = false;
            self.draw();
        });
    };

    TripMap.prototype.draw = function () {
        var view = this.view;
        if (!view.w || !view.h) {
            return;
        }
        var ctx = this.canvas.getContext("2d");
        ctx.setTransform(this.ratio || 1, 0, 0, this.ratio || 1, 0, 0);
        ctx.fillStyle = "#1a2c3d";
        ctx.fillRect(0, 0, view.w, view.h);
        var at = this.corner();
        var clear = [STREETS.clearOf(this.tools, this.canvas),
                     STREETS.clearOf(this.credit, this.canvas)];
        var drawn = this.tiles.draw(ctx, view.zoom, at.left, at.top, view.w, view.h, clear);
        if (STREETS.showCredit(this.credit, drawn)) {
            this.schedule();            // (names placed clear of it next time)
        }
        // The routes: each line on the copy of the world nearest the middle.
        var middle = at.left + view.w / 2;
        this.drawn = (this.options.lines || []).map(function (line) {
            var before = middle;
            return line.map(function (point) {
                var spot = MAP.project(point.lat, point.lon, at.w);
                before = MAP.nearestCopy(spot.x, before, at.w);
                return {sx: before - at.left, sy: spot.y - at.top, clip: point.clip, at: point.at};
            });
        });
        ROUTES.strokeLines(ctx, this.drawn, 5.5, ROUTES.HALO);
        ROUTES.strokeLines(ctx, this.drawn, 3, ROUTES.LINE);
        if (this.groups.zoom !== view.zoom) {
            this.groups = {zoom: view.zoom, list: MAP.cluster(this.pinPoints, at.w)};
        }
        this.pins = MAP.screenPins(this.groups.list, at.left, at.top, view.w, view.h, at.w);
        MAP.drawPins(ctx, this.pins);
        if (document.activeElement === this.canvas && this.byKey) {
            // (White on a dark edge: seen on the light street maps too.)
            ctx.beginPath();
            ctx.moveTo(view.w / 2 - 8, view.h / 2);
            ctx.lineTo(view.w / 2 + 8, view.h / 2);
            ctx.moveTo(view.w / 2, view.h / 2 - 8);
            ctx.lineTo(view.w / 2, view.h / 2 + 8);
            ctx.lineWidth = 4;
            ctx.strokeStyle = "rgba(12, 17, 24, 0.85)";
            ctx.stroke();
            ctx.lineWidth = 2;
            ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
            ctx.stroke();
        }
    };

    // What is under a screen point: {pin} or {route} or null.
    TripMap.prototype.hit = function (x, y, slack) {
        var pin = MAP.hitPin(this.pins, x, y, slack);
        if (pin) {
            return {pin: pin};
        }
        var route = this.drawn.length ? ROUTES.nearest(this.drawn, x, y, slack + 4) : null;
        return route ? {route: route} : null;
    };

    TripMap.prototype.open = function (found) {
        if (found && found.pin && this.options.onPin) {
            this.options.onPin(found.pin.group.ids.slice());
        } else if (found && found.route && this.options.onRoute) {
            this.options.onRoute(found.route);
        }
    };

    TripMap.prototype.listen = function () {
        var self = this;
        var canvas = this.canvas;
        var pointers = {};
        var drag = null;
        function point(event) {
            var rect = canvas.getBoundingClientRect();
            return {x: event.clientX - rect.left, y: event.clientY - rect.top};
        }
        function spread() {
            var list = Object.keys(pointers).map(function (key) {
                return pointers[key];
            });
            if (list.length < 2) {
                return null;
            }
            var dx = list[0].x - list[1].x;
            var dy = list[0].y - list[1].y;
            return {d: Math.sqrt(dx * dx + dy * dy), x: (list[0].x + list[1].x) / 2,
                    y: (list[0].y + list[1].y) / 2};
        }
        canvas.addEventListener("pointerdown", function (event) {
            if (event.button !== undefined && event.button !== 0) {
                return;
            }
            var at = point(event);
            pointers[event.pointerId] = at;
            self.byKey = false;
            drag = {x: at.x, y: at.y, moved: false, pinch: spread(), touch:
                    event.pointerType === "touch"};
            try {
                canvas.setPointerCapture(event.pointerId);
            } catch (error) {
                // (Not every browser lets it.)
            }
        });
        canvas.addEventListener("pointermove", function (event) {
            var at = point(event);
            if (!drag || !pointers[event.pointerId]) {
                var found = self.hit(at.x, at.y, 2);
                canvas.style.cursor = found ? "pointer" : "grab";
                return;
            }
            pointers[event.pointerId] = at;
            var pinch = spread();
            if (pinch && drag.pinch) {
                if (pinch.d > drag.pinch.d * 1.4 || pinch.d < drag.pinch.d / 1.4) {
                    self.zoomAt(pinch.x, pinch.y, self.view.zoom + (pinch.d > drag.pinch.d ? 1 : -1));
                    drag.pinch = pinch;
                }
                drag.moved = true;
                return;
            }
            if (pinch) {
                drag.pinch = pinch;
                return;
            }
            var dx = at.x - drag.x;
            var dy = at.y - drag.y;
            if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) {
                return;
            }
            drag.moved = true;
            canvas.style.cursor = "grabbing";
            self.panBy(dx, dy);
            self.fitted = false;
            drag.x = at.x;
            drag.y = at.y;
        });
        function release(event) {
            var was = drag;
            delete pointers[event.pointerId];
            if (Object.keys(pointers).length) {
                if (drag) {
                    drag.pinch = spread();
                }
                return;
            }
            drag = null;
            canvas.style.cursor = "grab";
            if (was && !was.moved && event.type === "pointerup") {
                var at = point(event);
                self.open(self.hit(at.x, at.y, was.touch ? 12 : 4));
            }
        }
        canvas.addEventListener("pointerup", release);
        canvas.addEventListener("pointercancel", release);
        canvas.addEventListener("dblclick", function (event) {
            var at = point(event);
            if (!self.hit(at.x, at.y, 2)) {
                self.zoomAt(at.x, at.y, self.view.zoom + (event.shiftKey ? -1 : 1));
                self.fitted = false;
            }
        });
        var wheel = new MAP.WheelZoom();
        canvas.addEventListener("wheel", function (event) {
            // The page scrolls; with Ctrl (or a pinch on a touchpad) the map zooms.
            if (!event.ctrlKey && !event.metaKey) {
                return;
            }
            event.preventDefault();
            var step = wheel.feed(event);
            if (step.steps) {
                var at = point(event);
                self.zoomAt(at.x, at.y, self.view.zoom + step.steps);
                self.fitted = false;
            }
        }, {passive: false});
        canvas.addEventListener("keydown", function (event) {
            var moves = {ArrowLeft: [80, 0], ArrowRight: [-80, 0], ArrowUp: [0, 80],
                         ArrowDown: [0, -80]};
            self.byKey = true;
            if (moves[event.key]) {
                self.panBy(moves[event.key][0], moves[event.key][1]);
                self.fitted = false;
            } else if (event.key === "+" || event.key === "=") {
                self.zoomAt(self.view.w / 2, self.view.h / 2, self.view.zoom + 1);
                self.fitted = false;
            } else if (event.key === "-" || event.key === "_") {
                self.zoomAt(self.view.w / 2, self.view.h / 2, self.view.zoom - 1);
                self.fitted = false;
            } else if (event.key === "0") {
                self.fit();
            } else if (event.key === "Enter") {
                self.open(self.hit(self.view.w / 2, self.view.h / 2, 40));
            } else {
                return;
            }
            event.preventDefault();
            self.schedule();
        });
        canvas.addEventListener("focus", function () {
            self.schedule();
        });
        canvas.addEventListener("blur", function () {
            self.byKey = false;
            self.schedule();
        });
    };

    /* ---------- The pages ---------------------------------------------------- */

    function startPage() {
        var el = SHELL.el;
        var clear = SHELL.clear;
        var adminOn = Boolean(SITE.capabilities.admin) && !SITE.capabilities.static;
        var config = {};
        try {
            config = JSON.parse(document.getElementById("app-config").textContent) || {};
        } catch (error) {
            config = {};
        }
        var data = null;            // {records, byId, index (made when needed), libraries,
                                    // albums}
        var trips = null;           // SITE.loadTrips' answer
        var locations = null;
        var routes = null;
        var wanted = typeof config.trip === "string" && config.trip ? config.trip
            : new URLSearchParams(window.location.search).get("trip") || "";

        function byId(id) {
            return document.getElementById(id);
        }

        function get(id) {
            return data ? data.byId[id] || null : null;
        }

        // The ids of a list the page shows (shown).
        function shownIds(ids) {
            return (ids || []).filter(function (id) {
                return shown(get(id), adminOn);
            });
        }

        // The search index of the pictures, made the first time it is needed (the
        // viewer's drives): the page itself needs none.
        function searchIndex() {
            if (data && !data.index) {
                data.index = SearchCore.prepare(data.records);
            }
            return data ? data.index : null;
        }

        // The text of a list of picture ids for an address, or "" when it is too long.
        function encodeIds(ids) {
            var text = data && SITE.idList ? SITE.idList.encode(ids, data.records) : "";
            return text && text.length <= SITE.idList.maxLength() ? text : "";
        }

        function libraryName(id) {
            var found = (data && data.libraries || []).filter(function (library) {
                return library.id === id;
            })[0];
            return found ? found.name : "";
        }

        function notice(kind, text) {
            var box = byId("trips-notices");
            clear(box);
            if (text) {
                box.appendChild(SHELL.notice(kind, text));
            }
        }

        function setHead(title, lead) {
            byId("trips-title").textContent = title;
            var leadNode = byId("trips-lead");
            clear(leadNode);
            SHELL.append(leadNode, lead);
        }

        /* The viewer */

        function driveOf(id) {
            var rec = get(id);
            if (!rec || rec.dv === undefined || rec.dv === null || !data) {
                return null;
            }
            var clips = searchIndex().driveClips(rec.dv, {library: "", hidden: adminOn});
            if (clips.length < 2 || clips.indexOf(id) < 0) {
                return null;
            }
            var text = null;
            clips.forEach(function (clipId) {
                var clip = get(clipId);
                if (text === null && clip && (clip.dvt || clip.dvd || (clip.dvg &&
                                                                       clip.dvg.length))) {
                    text = clipId;
                }
            });
            return {key: rec.dv, clips: clips, text: text, matched: {}, folded: false};
        }

        // (Whether the viewer's history entry is there: see showViewer.)
        var viewerPushed = false;

        function hashWithId(id) {
            return window.location.pathname + window.location.search +
                (id === null ? "" : "#id=" + encodeURIComponent(id));
        }

        function showViewer(ids, id, startAt) {
            ids = shownIds(ids);
            var index = Math.max(0, ids.indexOf(id));
            // (Closed, by Esc or by Back, the focus returns to what opened it.)
            var opener = document.activeElement;
            var opened = VIEWER.open({
                ids: ids,
                index: index,
                startAt: startAt || null,
                driveOf: driveOf,
                get: get,
                libraryName: libraryName,
                chipUrl: function (group, value) {
                    var state = group === "q" ? {q: value} : {filters: {}};
                    if (group !== "q") {
                        state.filters[group] = [value];
                    }
                    return SITE.searchUrl(state);
                },
                detailUrl: function (item) {
                    return SITE.detailUrl(item);
                },
                albums: function () {
                    return data ? data.albums : [];
                },
                onShow: function (shownId) {
                    if (viewerPushed) {
                        window.history.replaceState({tripViewer: true}, "", hashWithId(shownId));
                    }
                },
                onClose: function () {
                    if (viewerPushed) {
                        viewerPushed = false;
                        window.history.back();
                    }
                    window.setTimeout(function () {
                        if (opener && opener !== document.body && document.contains(opener)) {
                            opener.focus({preventScroll: true});
                        }
                    }, 0);
                },
                onHidden: function () {
                    reload();
                },
                onMarked: function () {
                    render();
                }
            });
            if (opened) {
                // Back closes the viewer (and stays on the page).
                window.history.pushState({tripViewer: true}, "", hashWithId(ids[index]));
                viewerPushed = true;
            }
        }

        /* Pieces */

        function thumb(rec, ids, label) {
            var badges = [];
            if (rec.mv) {
                badges.push(rec.dv !== undefined && rec.dv !== null ? "Drive" : "Video");
            }
            if (rec.p === "360") {
                badges.push("360\u00b0");
            }
            var words = VIEWER.altText(rec);
            var time = clock(rec);
            var button = el("button", {type: "button", className: "trip-thumb",
                                       title: words + (time ? DOT + time : ""),
                                       attrs: {"aria-label": (label ? label + ": " : "") + words +
                                               (badges.length ? " (" + badges.join(", ") + ")" : "") +
                                               (time ? ", " + time : "")},
                                       on: {click: function () {
                                           showViewer(ids, rec.i);
                                       }}}, [
                el("img", {src: SITE.thumbUrl(rec), alt: "", loading: "lazy", draggable: false})
            ]);
            if (badges.length) {
                button.appendChild(el("span", {className: "trip-thumb-badges",
                                               attrs: {"aria-hidden": "true"}},
                                      badges.map(function (text) {
                                          return el("span", {className: "tile-badge", text: text});
                                      })));
            }
            return button;
        }

        // ids: those the page shows (shownIds); looks: their look units.
        function strip(ids, count, label, looks) {
            var best = bestOf(ids, get, count, {looks: looks});
            if (!best.length && fallback(ids, get) !== null) {
                best = [fallback(ids, get)];
            }
            var node = el("div", {className: "trip-strip"});
            // (At most 8 a row: more go on another.)
            node.style.setProperty("--strip-count", String(Math.max(1, Math.min(8, count))));
            best.forEach(function (id) {
                node.appendChild(thumb(get(id), ids, label));
            });
            if (!best.length) {
                node.appendChild(el("p", {className: "trip-none", text: noneWords(ids, get)}));
            }
            return node;
        }

        function placesLine(names) {
            return el("p", {className: "trip-places"}, names.join(DOT));
        }

        function lengthWords(trip) {
            var count = dayCount(trip.start, trip.end);
            return count ? plural(count, "day") : "";
        }

        function sortedTrips() {
            return (trips && trips.trips || []).slice();
        }

        /* The story (trip_stories.py): a paragraph in each day's section, its
           links to pictures as small pictures that open the viewer; for the
           admin a bar to write it (a job), edit, hide or delete it. */

        var stories = {};           // trip id -> /api/trips/<id>/story's answer (null: loading)

        function loadStory(trip, again) {
            if (SITE.isStatic || !SITE.loadTripStory) {
                return;
            }
            if (!again && Object.prototype.hasOwnProperty.call(stories, trip.id)) {
                return;
            }
            stories[trip.id] = again ? stories[trip.id] || null : null;
            SITE.loadTripStory(trip.id, function (answer) {
                stories[trip.id] = answer || {trip: trip.id, days: []};
                if (wanted === trip.id) {
                    fillStory(trip);
                }
            });
        }

        function storyOf(trip) {
            return SITE.isStatic ? null : stories[trip.id] || null;
        }

        function storyLink(rec, cited) {
            var words = VIEWER.altText(rec);
            return el("button", {type: "button", className: "story-pic", title: words,
                                 attrs: {"aria-label": "Picture: " + words},
                                 on: {click: function () {
                                     showViewer(cited, rec.i);
                                 }}}, [
                el("img", {src: SITE.thumbUrl(rec), alt: "", loading: "lazy", draggable: false})
            ]);
        }

        // A paragraph of the story, its links as small pictures (those the page
        // shows; the viewer opens on the paragraph's pictures).
        function storyParagraph(text) {
            var parts = storyParts(text);
            var cited = [];
            parts.forEach(function (part) {
                var rec = part.id ? get(part.id) : null;
                if (rec && shown(rec, adminOn) && cited.indexOf(rec.i) < 0) {
                    cited.push(rec.i);
                }
            });
            var node = el("p", {className: "trip-story"});
            parts.forEach(function (part, index) {
                if (part.text !== undefined) {
                    node.appendChild(document.createTextNode(part.text));
                    return;
                }
                var rec = get(part.id);
                if (!rec || !shown(rec, adminOn)) {
                    return;
                }
                // (A mark right after a picture stays on its line: no "." alone on the next.)
                var next = parts[index + 1];
                var mark = next && next.text !== undefined
                    ? /^[.,;:!?)\u201d\u2019"']+/.exec(next.text) : null;
                if (mark) {
                    next.text = next.text.slice(mark[0].length);
                    node.appendChild(el("span", {className: "story-pic-tail"},
                                        [storyLink(rec, cited), mark[0]]));
                } else {
                    node.appendChild(storyLink(rec, cited));
                }
            });
            return node;
        }

        function fillStory(trip) {
            var story = storyOf(trip);
            var texts = storyDays(trip, story);
            (trip.days || []).forEach(function (day) {
                var slot = byId("story-" + day.date);
                if (!slot) {
                    return;
                }
                clear(slot);
                if (texts[day.date]) {
                    slot.appendChild(storyParagraph(texts[day.date]));
                }
                slot.classList.toggle("is-hidden", Boolean(story && story.hidden));
            });
            var bar = byId("trip-story-bar");
            if (bar) {
                clear(bar);
                if (adminOn && story && story.state) {
                    storyBar(bar, trip, story);
                }
                bar.hidden = !bar.firstChild;
            }
        }

        function storyBar(bar, trip, story) {
            var has = story.state !== "missing" && story.state !== "empty";
            bar.appendChild(el("h2", {className: "trip-story-title", text: "Story"}));
            bar.appendChild(el("p", {className: "trip-story-state" +
                                     (story.state === "stale" ? " is-stale" : ""),
                                     text: storyStateWords(story) + (has ? "" : " A language " +
                                         "model can write one from the trip's facts (text " +
                                         "only, no pictures).")}));
            var actions = el("div", {className: "trip-story-actions"});
            if (story.state !== "empty") {
                actions.appendChild(el("button", {
                    type: "button", className: "button small" + (has ? "" : " primary"),
                    text: has ? "Write again" : "Write the story",
                    title: has ? "A new story from the trip's facts (replaces this one)"
                        : "Ask the language model for a story of the trip",
                    on: {click: function () {
                        writeStories(trip, has);
                    }}}));
            }
            actions.appendChild(el("button", {type: "button", className: "button small",
                                              text: has ? "Edit\u2026" : "Write your own\u2026",
                                              on: {click: function () {
                                                  editStory(trip, story);
                                              }}}));
            if (has) {
                actions.appendChild(el("button", {
                    type: "button", className: "button small",
                    text: story.hidden ? "Show" : "Hide",
                    title: story.hidden ? "Show the story to everyone who sees the trip"
                        : "Only you see it (and no export publishes it)",
                    on: {click: function () {
                        changeStory(trip, {hidden: !story.hidden}, story.hidden
                            ? "The story shows again." : "The story is hidden.");
                    }}}));
                actions.appendChild(el("button", {
                    type: "button", className: "button small subtle", text: "Delete",
                    on: {click: function () {
                        SHELL.confirm({title: "Delete the story?", danger: true,
                                       message: "The story of " + trip.title + " is deleted " +
                                           "(the pictures are not touched). Write stories " +
                                           "writes a new one.",
                                       confirmText: "Delete"}).then(function (yes) {
                            if (yes) {
                                changeStory(trip, {forget: true}, "The story is deleted.");
                            }
                        });
                    }}}));
            }
            bar.appendChild(actions);
        }

        function changeStory(trip, body, message) {
            return SHELL.admin("/trips/" + encodeURIComponent(trip.id) + "/story", body)
                .then(function (reply) {
                    stories[trip.id] = reply.story;
                    if (message) {
                        SHELL.toast(message, "ok");
                    }
                    if (wanted === trip.id) {
                        fillStory(trip);
                    }
                    return reply;
                }, function () {
                    return null;
                });
        }

        function editStory(trip, story) {
            var texts = storyDays(trip, story);
            var box = SHELL.modal({title: "The story of " + trip.title, wide: true});
            box.body.appendChild(el("p", {className: "field-hint", text: "A paragraph a day " +
                "(a day left empty has none). [#123] links picture 123: keep, move or delete " +
                "them. The story stays as you write it: Write stories leaves it alone."}));
            var areas = [];
            (trip.days || []).forEach(function (day) {
                var number = trip.start ? dayCount(trip.start, day.date) : 1;
                var area = el("textarea", {className: "input trip-story-edit", rows: 4,
                                           value: texts[day.date] || "", maxLength: 3000});
                areas.push({date: day.date, area: area});
                box.body.appendChild(SHELL.field("Day " + number + DOT + dayWords(day.date),
                                                 area).node);
            });
            box.button("Cancel", "", box.close);
            box.button("Save", "primary", function () {
                changeStory(trip, {days: areas.map(function (item) {
                    return {date: item.date, text: item.area.value};
                })}, "Saved the story.").then(function (reply) {
                    if (reply) {
                        box.close();
                    }
                });
            });
        }

        var FINISHED = ["done", "failed", "cancelled", "interrupted"];

        // Call done(job) once the job has finished (the header's indicator shows it
        // meanwhile).
        function watchJob(jobId, done) {
            function look() {
                SHELL.admin("/jobs?log=0", undefined, {quiet: true}).then(function (snapshot) {
                    var found = null;
                    [snapshot.current].concat(snapshot.queue || [], snapshot.recent || [])
                        .forEach(function (job) {
                            if (job && job.id === jobId && !found) {
                                found = job;
                            }
                        });
                    if (found && FINISHED.indexOf(found.state) >= 0) {
                        done(found);
                    } else {
                        window.setTimeout(look, 3000);
                    }
                }, function () {
                    window.setTimeout(look, 10000);
                });
            }
            window.setTimeout(look, 1500);
        }

        // Write the story of a trip (redo: again) or of every trip that needs one
        // (trip null), after showing what it takes (plan).
        function writeStories(trip, redo) {
            var query = (trip ? "?trip=" + encodeURIComponent(trip.id) + "&" : "?") +
                (redo ? "redo=1" : "redo=0");
            SHELL.admin("/trips/stories/plan" + query).then(function (plan) {
                var lines = planWords(plan);
                if (!plan.trips.length) {
                    SHELL.toast(lines.join(" "), "info");
                    return;
                }
                SHELL.confirm({
                    title: trip ? (redo ? "Write the story again?" : "Write the story?")
                        : "Write the trips' stories?",
                    message: lines.map(function (line) {
                        return el("p", {text: line});
                    }).concat(trip && redo && (storyOf(trip) || {}).edited
                        ? [el("p", {className: "field-hint",
                                    text: "Your edits of this story are replaced."})] : []),
                    confirmText: "Write"
                }).then(function (yes) {
                    if (!yes) {
                        return;
                    }
                    var params = {redo: Boolean(redo)};
                    if (trip) {
                        params.trip = trip.id;
                    }
                    SHELL.admin("/jobs", {kind: "trip_stories", library_id: null,
                                          params: params}).then(function (reply) {
                        SHELL.toast((trip ? "Writing the story" : "Writing the stories") +
                                    " (the header shows how it goes).", "info");
                        watchJob(reply.job.id, function (job) {
                            var words = SHELL.jobs.summary(job);
                            SHELL.toast((job.state === "done" ? "" : SHELL.jobs.state(job.state) +
                                         ": ") + (words || job.error || "finished") + ".",
                                        job.state === "done" ? "ok" : "warn");
                            stories = {};
                            if (wanted) {
                                render();
                            }
                        });
                    }, function () {});
                });
            }, function () {});
        }

        /* The list */

        function card(trip) {
            var all = shownIds(tripIds(trip));
            var cover = bestOf(all, get, COVER_COUNT, {perDay: true, looks: tripLooks(trip)});
            var href = SITE.tripUrl(trip.id);
            var node = el("article", {className: "card trip-card" + (trip.hidden ? " is-hidden"
                                                                         : "")});
            // (The title's link is the one to tab to: the cover is the same link.)
            var link = el("a", {className: "trip-cover cover-" + Math.max(1, cover.length),
                                href: href, tabIndex: -1, attrs: {"aria-hidden": "true"}});
            cover.forEach(function (id) {
                link.appendChild(el("img", {src: SITE.thumbUrl(get(id)), alt: "",
                                            loading: "lazy"}));
            });
            var words = factsWords(facts(all, get)) || plural(all.length, "picture");
            if (!cover.length) {
                link.appendChild(el("span", {className: "trip-cover-empty", text: words}));
            }
            node.appendChild(link);
            var head = el("div", {className: "trip-card-head"});
            head.appendChild(el("h3", {className: "trip-card-title"}, [
                el("a", {href: href, text: trip.title, attrs: {"data-trip": trip.id}})
            ]));
            var bits = [];
            if (trip.start) {
                bits.push(spanWords(trip.start, trip.end));
            }
            bits.push(words);
            head.appendChild(el("p", {className: "trip-card-facts", text: bits.join(DOT)}));
            if (trip.places && trip.places.length) {
                head.appendChild(el("p", {className: "trip-card-places",
                                          text: trip.places.slice(0, 6).map(function (place) {
                                              return place.name;
                                          }).join(DOT) + (trip.places.length > 6 ? " \u2026" : "")}));
            }
            node.appendChild(head);
            if (adminOn && trip.hidden) {
                node.appendChild(el("div", {className: "trip-card-actions"}, [
                    el("button", {type: "button", className: "button small", text: "Show again",
                                  on: {click: function () {
                                      change("/trips/" + encodeURIComponent(trip.id),
                                             {hidden: false}, "The trip shows again.");
                                  }}})
                ]));
            }
            return node;
        }

        function renderList() {
            document.title = "Trips - " + (SITE.title || "Image Search");
            byId("trips-back").hidden = true;
            clear(byId("trips-actions"));
            if (adminOn && trips.enabled && sortedTrips().length) {
                byId("trips-actions").appendChild(el("button", {
                    type: "button", className: "button", text: "Write stories\u2026",
                    title: "A story for each trip that has none or an out-of-date one, " +
                        "written by a language model from the trip's facts",
                    on: {click: function () {
                        writeStories(null, false);
                    }}}));
            }
            setHead("Trips", adminOn
                ? "Journeys found in the pictures: libraries whose folder is named like a trip " +
                  "(a date first), and runs of days away from home. Open one to rename, hide, " +
                  "merge or split it."
                : "Journeys found in the pictures. Open one to see its days and its map.");
            var body = byId("trips-body");
            clear(body);
            if (!trips.enabled) {
                body.appendChild(SHELL.notice("info", adminOn ? "Trips are turned off " +
                                              "(\"trips\": {\"enabled\": false} in " +
                                              "config.json)." : "There are no trips to see."));
                return;
            }
            var shown = sortedTrips().filter(function (trip) {
                return !trip.hidden;
            });
            var hidden = sortedTrips().filter(function (trip) {
                return trip.hidden;
            });
            if (!shown.length && !hidden.length) {
                body.appendChild(el("div", {className: "card trip-none-card"}, [
                    el("p", {text: "No trips were found yet."}),
                    el("p", {className: "field-hint", text: "A library whose folder starts " +
                             "with a date (\"2024-05-12 - Lakes\") is a trip, and so are days " +
                             "with pictures taken far from home (their GPS positions)."})
                ]));
                return;
            }
            var year = null;
            var grid = null;
            shown.forEach(function (trip) {
                var thisYear = (trip.start || "").slice(0, 4) || "Undated";
                if (thisYear !== year) {
                    year = thisYear;
                    body.appendChild(el("h2", {className: "trip-year", text: year}));
                    grid = el("div", {className: "trip-grid"});
                    body.appendChild(grid);
                }
                grid.appendChild(card(trip));
            });
            if (hidden.length) {
                var details = el("details", {className: "trip-hidden"}, [
                    el("summary", {text: "Hidden trips (" + hidden.length + ")"})
                ]);
                var hiddenGrid = el("div", {className: "trip-grid"});
                hidden.forEach(function (trip) {
                    hiddenGrid.appendChild(card(trip));
                });
                details.appendChild(hiddenGrid);
                body.appendChild(details);
            }
        }

        /* One trip */

        function neighbours(trip) {
            var list = sortedTrips().filter(function (item) {
                return item.start;
            }).sort(function (a, b) {
                return a.start < b.start ? -1 : a.start > b.start ? 1 : 0;
            });
            var at = list.map(function (item) {
                return item.id;
            }).indexOf(trip.id);
            return {before: at > 0 ? list[at - 1] : null,
                    after: at >= 0 && at < list.length - 1 ? list[at + 1] : null};
        }

        // True when a trip is a part of a split trip, or was split.
        function isSplit(trip) {
            return Boolean(trip.base && trip.base !== trip.id) ||
                sortedTrips().some(function (item) {
                    return item.base === trip.id && item.id !== trip.id;
                });
        }

        function renderActions(trip) {
            var actions = byId("trips-actions");
            clear(actions);
            // (An export's search page holds the trip's pictures only.)
            var search = SITE.searchUrl(SITE.isStatic ? {sort: "oldest"}
                : tripSearch(trip, shownIds(tripIds(trip)), encodeIds));
            actions.appendChild(el("a", {className: "button primary", href: search,
                                         text: "Open in search"}));
            var mapHref = SITE.mapUrl ? SITE.mapUrl(SITE.isStatic ? {} : mapSearch(trip)) : null;
            if (mapHref && hasPlaces(trip)) {
                actions.appendChild(el("a", {className: "button", href: mapHref,
                                             text: "On the big map"}));
            }
            if (!adminOn) {
                return;
            }
            actions.appendChild(el("button", {type: "button", className: "button",
                                              text: "Rename", on: {click: function () {
                                                  rename(trip);
                                              }}}));
            actions.appendChild(el("button", {type: "button", className: "button",
                                              text: trip.hidden ? "Show again" : "Hide",
                                              title: trip.hidden ? "List it among the trips again"
                                                  : "Leave it off the list (and off others' " +
                                                    "pages)", on: {click: function () {
                                                  change("/trips/" + encodeURIComponent(trip.id),
                                                         {hidden: !trip.hidden},
                                                         trip.hidden ? "The trip shows again."
                                                             : "The trip is hidden. It is " +
                                                               "listed under Hidden trips.");
                                              }}}));
            var near = neighbours(trip);
            [["before", "Merge with the trip before"], ["after", "Merge with the trip after"]]
                .forEach(function (pair) {
                    var other = near[pair[0]];
                    if (!other) {
                        return;
                    }
                    var parts = other.base && trip.base && other.base === trip.base;
                    if (!parts && (isSplit(trip) || isSplit(other))) {
                        // (The server refuses it: the parts are joined again first.)
                        return;
                    }
                    actions.appendChild(el("button", {
                        type: "button", className: "button",
                        text: parts ? (pair[0] === "before" ? "Join with the part before"
                            : "Join with the part after") : pair[1],
                        title: other.title + " (" + spanWords(other.start, other.end) + ")",
                        on: {click: function () {
                            merge(trip, other);
                        }}}));
                });
            actions.appendChild(el("a", {className: "button", text: "Export...",
                                         href: "/manage?dialog=trip-export&id=" +
                                             encodeURIComponent(trip.id),
                                         title: "Publish the trip as a website of its own " +
                                             "(the export dialog on the Manage page)"}));
            if (trip.edited || trip.merged || isSplit(trip)) {
                actions.appendChild(el("button", {type: "button", className: "button subtle",
                                                  text: "As found",
                                                  title: "Forget the title, merges and splits " +
                                                      "you made: show the trip as it was found",
                                                  on: {click: function () {
                                                      reset(trip);
                                                  }}}));
            }
        }

        function hasPlaces(trip) {
            return Boolean(trip.places && trip.places.length) || tripPoints(trip, locations)
                .length > 0;
        }

        function mapSection(trip) {
            var section = el("section", {className: "card trip-map-card",
                                         attrs: {"aria-label": "Map"}});
            if (!SITE.tileUrl(0, 0, 0)) {
                return null;
            }
            var points = tripPoints(trip, locations || []).filter(function (point) {
                return shown(get(point.id), adminOn);
            });
            var ids = {};
            shownIds(tripIds(trip)).forEach(function (id) {
                ids[id] = true;
            });
            var lines = [];
            (routes ? routes.routes : []).forEach(function (route) {
                ROUTES.lines(route, function (id) {
                    return ids[id] === true;
                }).forEach(function (line) {
                    lines.push(line);
                });
            });
            if (!points.length && !lines.length) {
                return null;
            }
            var map = new TripMap({points: points, lines: lines, onPin: function (pinIds) {
                pinIds.sort(function (a, b) {
                    var x = get(a);
                    var y = get(b);
                    return x && y ? compareTime(x, y) : 0;
                });
                showViewer(pinIds, pinIds[0]);
            }, onRoute: function (hit) {
                var drive = driveOf(hit.clip);
                var clips = drive ? drive.clips : [hit.clip];
                showViewer(clips, hit.clip, {id: hit.clip, seconds: hit.at});
            }});
            section.appendChild(map.node);
            var legend = el("p", {className: "trip-map-legend"});
            // (The pins: the clips on a route have none, see TripMap.)
            var estimated = map.pinPoints.filter(function (point) {
                return point.e;
            }).length;
            var located = map.pinPoints.length - estimated;
            var clips = points.length - map.pinPoints.length;
            var parts = [];
            if (located) {
                parts.push(el("span", {className: "legend-pin"}, plural(located,
                                                                         "located picture")));
            }
            if (estimated) {
                parts.push(el("span", {className: "legend-pin is-estimated"},
                              plural(estimated, "estimated place")));
            }
            if (lines.length) {
                parts.push(el("span", {className: "legend-route", text: "drive routes" +
                                       (clips ? " (" + plural(clips, "clip") + ")" : "")}));
            }
            var far = points.length - fitPoints(points).length;
            if (far) {
                parts.push(el("span", {className: "trip-map-far",
                                       text: plural(far, "picture") + " far away (zoom out)"}));
            }
            parts.forEach(function (part) {
                legend.appendChild(part);
            });
            legend.appendChild(el("span", {className: "trip-map-hint",
                                           text: "Drag to move, Ctrl + wheel to zoom"}));
            section.appendChild(legend);
            return section;
        }

        function daySection(trip, day, number, withLinks) {
            var ids = shownIds(day.ids);
            var section = el("section", {className: "card trip-day",
                                         id: "day-" + day.date, tabIndex: -1,
                                         attrs: {"aria-labelledby": "day-title-" + day.date}});
            var head = el("div", {className: "trip-day-head"});
            head.appendChild(el("h2", {id: "day-title-" + day.date, className: "trip-day-title"},
                                [el("span", {className: "trip-day-number", text: "Day " + number}),
                                 " " + dayWords(day.date)]));
            var actions = el("div", {className: "trip-day-actions"});
            actions.appendChild(el("a", {className: "button small",
                                         href: SITE.searchUrl(daySearch(trip, day, number, ids,
                                                                        get, encodeIds)),
                                         text: "Open in search"}));
            if (withLinks) {
                // (Back to the row of days at the top.)
                actions.appendChild(el("a", {className: "button small subtle", href: "#trip-days",
                                             text: "\u2191 Days",
                                             attrs: {"aria-label": "Back to the days"}}));
            }
            if (adminOn && number > 1) {
                actions.appendChild(el("button", {type: "button", className: "button small subtle",
                                                  text: "Split here",
                                                  title: "Make this day and the ones after it " +
                                                      "a trip of their own",
                                                  on: {click: function () {
                                                      split(trip, day);
                                                  }}}));
            }
            head.appendChild(actions);
            section.appendChild(head);
            if (day.places && day.places.length) {
                section.appendChild(placesLine(day.places));
            }
            section.appendChild(el("div", {className: "trip-story-slot",
                                           id: "story-" + day.date}));
            section.appendChild(el("p", {className: "trip-day-facts",
                                         text: factsWords(facts(ids, get))}));
            section.appendChild(strip(ids, trips.best_per_day || 6, "Day " + number, day.looks));
            return section;
        }

        function otherSection(trip, ids) {
            var section = el("section", {className: "card trip-day trip-other",
                                         id: "trip-other", tabIndex: -1,
                                         attrs: {"aria-labelledby": "trip-other-title"}});
            var head = el("div", {className: "trip-day-head"});
            head.appendChild(el("h2", {id: "trip-other-title", className: "trip-day-title",
                                       text: "Other pictures"}));
            head.appendChild(el("div", {className: "trip-day-actions"}, [
                el("a", {className: "button small", text: "Open in search",
                         href: SITE.searchUrl(otherSearch(trip, ids, get, encodeIds))})
            ]));
            section.appendChild(head);
            section.appendChild(el("p", {className: "trip-day-facts", text:
                factsWords(facts(ids, get)) + DOT + "no date of their own within the " +
                "trip's (a file's date only, or a camera's clock set wrong)"}));
            section.appendChild(strip(ids, trips.best_per_day || 6, "Other pictures",
                                      trip.other_looks));
            return section;
        }

        /* A row of links to the days of a long trip (and back to the top from each). */
        function dayLinks(trip, others) {
            var nav = el("nav", {className: "trip-day-links", id: "trip-days", tabIndex: -1,
                                 attrs: {"aria-label": "Days"}});
            (trip.days || []).forEach(function (day) {
                var parsed = parseDay(day.date);
                var number = trip.start ? dayCount(trip.start, day.date) : 1;
                nav.appendChild(el("a", {href: "#day-" + day.date, title: dayWords(day.date)},
                                   [el("span", {className: "trip-day-number",
                                                text: "Day " + number}),
                                    parsed ? " " + parsed.day + " " +
                                        MONTHS[parsed.month - 1].slice(0, 3) : ""]));
            });
            if (others) {
                nav.appendChild(el("a", {href: "#trip-other", text: "Other pictures"}));
            }
            return nav;
        }

        function renderTrip(trip) {
            // (A trip's export is the trip: no list of trips to go back to.)
            document.title = SITE.isStatic ? trip.title
                : trip.title + " - Trips - " + (SITE.title || "Image Search");
            var back = byId("trips-back");
            back.hidden = SITE.isStatic;
            byId("trips-back-link").href = SITE.tripsUrl() || "#";
            var lead = [];
            var bits = [];
            if (trip.start) {
                bits.push(spanWords(trip.start, trip.end));
                bits.push(lengthWords(trip));
            }
            var all = shownIds(tripIds(trip));
            bits.push(factsWords(facts(all, get)) || plural(all.length, "picture"));
            lead.push(el("span", {className: "trip-dates", text: bits.join(DOT)}));
            if (adminOn && trip.hidden) {
                lead.push(el("span", {className: "trip-flag", text: "Hidden"}));
            }
            setHead(trip.title, lead);
            renderActions(trip);
            var body = byId("trips-body");
            clear(body);
            if (trip.places && trip.places.length) {
                body.appendChild(el("p", {className: "trip-route-places"}, [
                    el("span", {className: "visually-hidden", text: "Places, in order: "}),
                    trip.places.map(function (place) {
                        return place.name;
                    }).join(" \u2192 ")
                ]));
            }
            if (adminOn) {
                body.appendChild(el("section", {className: "card trip-story-card",
                                                id: "trip-story-bar", hidden: true,
                                                attrs: {"aria-label": "Story"}}));
            }
            var others = shownIds(trip.other);
            var withLinks = (trip.days || []).length > DAY_LINKS_FROM;
            if (withLinks) {
                body.appendChild(dayLinks(trip, others.length > 0));
            }
            var map = mapSection(trip);
            if (map) {
                body.appendChild(map);
            }
            (trip.days || []).forEach(function (day) {
                var number = trip.start ? dayCount(trip.start, day.date) : 1;
                body.appendChild(daySection(trip, day, number, withLinks));
            });
            if (others.length) {
                body.appendChild(otherSection(trip, others));
            }
            fillStory(trip);
            loadStory(trip);
        }

        /* Changes (admin) */

        function reload(done) {
            SITE.refreshTrips(function (loaded) {
                if (loaded) {
                    trips = loaded;
                }
                render();
                if (done) {
                    done();
                }
            });
        }

        function change(path, body, message, next) {
            return SHELL.admin(path, body).then(function (reply) {
                if (message) {
                    SHELL.toast(message, "ok");
                }
                if (next) {
                    next(reply);
                }
                reload();
                return reply;
            }, function () {});
        }

        function rename(trip) {
            var box = SHELL.modal({title: "Rename " + trip.title});
            var input = el("input", {type: "text", className: "input", maxLength: 100,
                                     autocomplete: "off", spellcheck: false, value: trip.title});
            var field = SHELL.field("Trip name", input, {hint: trip.auto_title && trip.edited
                ? "Leave it empty for the name it was found with (" + trip.auto_title + ")."
                : "Leave it empty for the name it was found with."});
            box.body.appendChild(field.node);
            box.button("Cancel", "", function () {
                box.close();
            });
            function go() {
                var name = input.value.replace(/\s+/g, " ").replace(/^\s+|\s+$/g, "");
                box.close();
                change("/trips/" + encodeURIComponent(trip.id), {title: name},
                       name ? "Renamed the trip." : "The trip has the name it was found with.");
            }
            box.button("Rename", "primary", go);
            box.submitOnEnter(go);
            window.setTimeout(function () {
                input.focus();
                input.select();
            }, 0);
        }

        function merge(trip, other) {
            var parts = other.base && trip.base && other.base === trip.base;
            SHELL.confirm({
                title: parts ? "Join the parts?" : "Merge the trips?",
                message: parts ? "The two parts become one trip again."
                    : "\u201c" + trip.title + "\u201d and \u201c" + other.title + "\u201d (" +
                      spanWords(other.start, other.end) + ") become one trip. \u201cAs found\u201d " +
                      "takes them apart again.",
                confirmText: parts ? "Join" : "Merge"
            }).then(function (yes) {
                if (!yes) {
                    return;
                }
                change("/trips/merge", {ids: [trip.id, other.id]},
                       parts ? "Joined the parts." : "Merged the trips.", function (reply) {
                           if (reply && reply.id && reply.id !== trip.id) {
                               go(reply.id, true);
                           }
                       });
            });
        }

        function split(trip, day) {
            SHELL.confirm({
                title: "Split the trip?",
                message: dayWords(day.date) + " and the days after it become a trip of their " +
                    "own. \u201cJoin with the part after\u201d puts them back.",
                confirmText: "Split"
            }).then(function (yes) {
                if (yes) {
                    change("/trips/" + encodeURIComponent(trip.id) + "/split", {date: day.date},
                           "Split the trip.");
                }
            });
        }

        function reset(trip) {
            SHELL.confirm({
                title: "Show the trip as found?",
                message: "Its title, its merges and its splits are forgotten (the pictures " +
                    "are not touched).",
                confirmText: "As found"
            }).then(function (yes) {
                if (yes) {
                    change("/trips/" + encodeURIComponent(trip.id) + "/reset", {},
                           "The trip shows as it was found.");
                }
            });
        }

        /* Navigation */

        function render() {
            if (!data || !trips) {
                return;
            }
            if (!wanted) {
                renderList();
                return;
            }
            var trip = sortedTrips().filter(function (item) {
                return item.id === wanted;
            })[0];
            if (!trip) {
                // A part that was joined, or a trip merged into another: its first part.
                var base = wanted.split(".")[0];
                trip = sortedTrips().filter(function (item) {
                    return item.id === base || (item.merged || []).indexOf(base) >= 0;
                })[0];
            }
            if (!trip) {
                notice("warn", "There is no such trip (any more). Here are the trips there are.");
                wanted = "";
                window.history.replaceState(null, "", SITE.tripsUrl() || "");
                renderList();
                return;
            }
            if (trip.id !== wanted) {
                wanted = trip.id;
                window.history.replaceState(null, "", SITE.tripUrl(trip.id));
            }
            renderTrip(trip);
        }

        /* After moving to another trip or to the list within the page: the focus on
           the new page's title (else it stays on a link that is gone, and Tab goes
           on from there), or -- back on the list -- on the link of the trip left. */
        function focusPage(left) {
            var target = null;
            if (!wanted && left) {
                target = document.querySelector('.trip-card-title a[data-trip="' +
                                                String(left).replace(/["\\]/g, "") + '"]');
            }
            if (!target) {
                target = byId("trips-title");
                target.setAttribute("tabindex", "-1");
            }
            try {
                target.focus({preventScroll: true});
            } catch (error) {
                target.focus();
            }
        }

        function go(id, replace) {
            var left = wanted;
            wanted = id;
            var url = id ? SITE.tripUrl(id) : SITE.tripsUrl();
            if (url) {
                window.history[replace ? "replaceState" : "pushState"](null, "", url);
            }
            notice("", "");
            render();
            window.scrollTo(0, 0);
            focusPage(left);
        }

        function start() {
            var waiting = 4;
            function done() {
                waiting -= 1;
                if (!waiting) {
                    if (!trips) {
                        clear(byId("trips-body"));
                        byId("trips-body").appendChild(SHELL.notice(
                            "error", "The trips could not be loaded. Is the server still running?"));
                        return;
                    }
                    render();
                }
            }
            SITE.loadIndex(function (loaded) {
                if (loaded) {
                    var lookup = {};
                    loaded.records.forEach(function (rec) {
                        lookup[rec.i] = rec;
                    });
                    data = {records: loaded.records, byId: lookup,
                            libraries: loaded.libraries || [],
                            albums: Array.isArray(loaded.albums) ? loaded.albums : [],
                            index: null};
                } else {
                    data = {records: [], byId: {}, libraries: [], albums: [], index: null};
                }
                done();
            });
            SITE.loadTrips(function (loaded) {
                trips = loaded;
                useMoments(loaded && loaded.trips);
                done();
            });
            SITE.loadLocations(function (loaded) {
                locations = loaded || [];
                done();
            });
            SITE.loadRoutes(function (loaded) {
                routes = ROUTES.prepare(loaded || []);
                done();
            });
            window.addEventListener("popstate", function () {
                if (VIEWER.isOpen() && !/(?:^#|&)id=/.test(window.location.hash || "")) {
                    // (Back from the viewer: it closes, the page stays.)
                    viewerPushed = false;
                    VIEWER.close();
                    return;
                }
                var left = wanted;
                var path = window.location.pathname;
                var match = /\/trip\/([^\/?#]+)/.exec(path);
                // (A trip's export is its one trip: config.trip.)
                wanted = match ? decodeURIComponent(match[1])
                    : new URLSearchParams(window.location.search).get("trip") ||
                      (SITE.isStatic && typeof config.trip === "string" ? config.trip : "");
                if (wanted === left) {
                    return;     // (a day's link: #day-..., or the viewer's address)
                }
                render();
                focusPage(left);
            });
            document.addEventListener("click", function (event) {
                // Links to the trips stay on this page (no reload).
                var link = event.target && event.target.closest ? event.target.closest("a") : null;
                if (!link || event.button !== 0 || event.ctrlKey || event.metaKey ||
                        event.shiftKey || event.altKey) {
                    return;
                }
                var href = link.getAttribute("href") || "";
                var match = /^\/trip\/([^\/?#]+)$/.exec(href);
                if (match) {
                    event.preventDefault();
                    go(decodeURIComponent(match[1]), false);
                } else if (href === "/trips" && link.closest("#trips")) {
                    event.preventDefault();
                    go("", false);
                }
            });
        }

        start();
    }

    if (hasDocument && document.getElementById("trips-body")) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", startPage);
        } else {
            startPage();
        }
    }

    return {
        SPREAD_SECONDS: SPREAD_SECONDS,
        SAME_MOMENT_SECONDS: SAME_MOMENT_SECONDS,
        moment: moment,
        dayMoments: dayMoments,
        useMoments: useMoments,
        parseDay: parseDay,
        dayWords: dayWords,
        spanWords: spanWords,
        dayCount: dayCount,
        shown: shown,
        eligible: eligible,
        demoted: demoted,
        quality: quality,
        compareRank: compareRank,
        unitKeys: unitKeys,
        bestOf: bestOf,
        fallback: fallback,
        noneWords: noneWords,
        facts: facts,
        factsWords: factsWords,
        librariesOf: librariesOf,
        idsSearch: idsSearch,
        mapSearch: mapSearch,
        tripSearch: tripSearch,
        daySearch: daySearch,
        otherSearch: otherSearch,
        tripPoints: tripPoints,
        fitPoints: fitPoints,
        tripLooks: tripLooks,
        tripIds: tripIds,
        storyParts: storyParts,
        storyDays: storyDays,
        planWords: planWords,
        storyStateWords: storyStateWords
    };
})();

if (typeof module === "object" && module && module.exports) {
    module.exports = TRIPS;
}
