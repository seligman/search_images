/* Tests for the parts of static/trips.js that need no page: dates in words,
   the best pictures of a day (what may be shown, the order of preference,
   stacks and drives counted once, spread over the day), what a day holds,
   the searches of a trip and its days, and the points its map fits.

   Run by tests/test_trips.py, or directly: node tests/js/trips_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var TRIPS = require(path.join(__dirname, "..", "..", "static", "trips.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

// Records by id, as SITE.loadIndex gives them (keys: see search_index.py).
function lookup(records) {
    var byId = {};
    records.forEach(function (rec) {
        byId[rec.i] = rec;
    });
    return function (id) {
        return byId[id] || null;
    };
}

function at(hour, minute) {
    return "2024-05-11T" + (hour < 10 ? "0" : "") + hour + ":" + (minute < 10 ? "0" : "") +
        minute + ":00";
}

test("dates in words", function () {
    assert.strictEqual(TRIPS.dayWords("2024-11-09"), "Saturday, 9 November 2024");
    assert.strictEqual(TRIPS.dayWords("2024-02-30"), "");
    assert.strictEqual(TRIPS.spanWords("2024-11-09", "2024-11-15"), "9\u201315 November 2024");
    assert.strictEqual(TRIPS.spanWords("2024-10-30", "2024-11-02"),
                       "30 October \u2013 2 November 2024");
    assert.strictEqual(TRIPS.spanWords("2023-12-30", "2024-01-02"),
                       "30 December 2023 \u2013 2 January 2024");
    assert.strictEqual(TRIPS.spanWords("2024-11-09", "2024-11-09"), "9 November 2024");
    assert.strictEqual(TRIPS.dayCount("2024-02-27", "2024-03-02"), 5);
    assert.strictEqual(TRIPS.dayCount("", "2024-03-02"), 0);
});

test("hidden, private, sensitive pictures and documents are never among the best", function () {
    var good = {i: 1, ty: "photo", q: 4};
    assert.ok(TRIPS.eligible(good));
    [{i: 2, h: 1}, {i: 3, pr: 1}, {i: 4, se: "suggestive"}, {i: 5, ty: "screenshot"},
     {i: 6, ty: "document"}, {i: 7, dt: "card_or_id"}, {i: 9, mv: 1, ty: "video", vt: "screenshot"},
     null].forEach(function (rec) {
        assert.ok(!TRIPS.eligible(rec), JSON.stringify(rec));
    });
    // A sign, a menu, a photo of a screen or one marked accidental may be among the
    // best: after the others.
    [{i: 8, qf: ["accidental"]}, {i: 10, qf: ["screen_photo"]}, {i: 12, dt: "sign"},
     {i: 13, dt: "menu"}, {i: 14, dt: "other"}].forEach(function (rec) {
        assert.ok(TRIPS.eligible(rec), JSON.stringify(rec));
        assert.ok(TRIPS.demoted(rec), JSON.stringify(rec));
    });
    assert.ok(TRIPS.eligible({i: 11, se: "none", dt: "none", qf: ["blurry"]}));
    assert.ok(!TRIPS.demoted({i: 11, se: "none", dt: "none", qf: ["blurry"]}));
    var get = lookup([good, {i: 2, h: 1, fav: 1}, {i: 3, pr: 1, rt: 5}]);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3], get, 6), [1]);
});

test("a sign or a screen comes after the others, unless it is a favorite or rated", function () {
    var records = [
        {i: 1, q: 5, sh: 5, dt: "sign", ts: at(9, 0)},
        {i: 2, q: 3, sh: 3, ts: at(11, 0)},
        {i: 3, q: 5, sh: 5, qf: ["screen_photo"], ts: at(13, 0)},
        {i: 4, q: 4, sh: 4, dt: "menu", fav: 1, ts: at(15, 0)}
    ];
    var get = lookup(records);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4], get, 2), [2, 4]);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4], get, 3), [1, 2, 4]);
    assert.ok(!TRIPS.demoted(records[3]), "a favorite is not demoted");
    // A day of signs only still shows them.
    assert.deepStrictEqual(TRIPS.bestOf([1, 3], get, 6), [1, 3]);
});

test("a day with nothing to be among its best shows its best that may be shown", function () {
    var records = [
        {i: 1, ty: "document", dt: "whiteboard", q: 2, ts: at(9, 0)},
        {i: 2, ty: "screenshot", q: 4, ts: at(10, 0)},
        {i: 3, pr: 1, q: 5, ts: at(11, 0)},
        {i: 4, se: "suggestive", q: 5, ts: at(12, 0)},
        {i: 5, h: 1, q: 5, ts: at(13, 0)}
    ];
    var get = lookup(records);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4, 5], get, 6), []);
    assert.strictEqual(TRIPS.fallback([1, 2, 3, 4, 5], get), 2);
    assert.strictEqual(TRIPS.fallback([3, 4, 5], get), null);
    // Nothing at all: the reasons there are, and only those.
    assert.strictEqual(TRIPS.noneWords([3], get), "Nothing to show here: its picture is private.");
    assert.strictEqual(TRIPS.noneWords([3, 4], get),
                       "Nothing to show here: its pictures are private or sensitive.");
    assert.strictEqual(TRIPS.noneWords([3, 4, 5], get),
                       "Nothing to show here: its pictures are hidden, private or sensitive.");
    assert.strictEqual(TRIPS.noneWords([], get), "Nothing to show here.");
});

test("the page shows what the search page shows by default", function () {
    assert.ok(TRIPS.shown({i: 1}, false));
    assert.ok(TRIPS.shown({i: 1, se: "suggestive"}, false));
    assert.ok(!TRIPS.shown({i: 1, se: "explicit"}, false));
    assert.ok(!TRIPS.shown({i: 1, se: "graphic"}, true));
    assert.ok(!TRIPS.shown({i: 1, h: 1}, false));
    assert.ok(TRIPS.shown({i: 1, h: 1}, true));
    assert.ok(!TRIPS.shown(null, true));
});

test("favorites, then ratings, then the better looking", function () {
    var records = [
        {i: 1, q: 5, sh: 5, ts: at(9, 0)},
        {i: 2, q: 2, sh: 2, fav: 1, ts: at(10, 0)},
        {i: 3, q: 3, sh: 3, rt: 4, ts: at(11, 0)},
        {i: 4, q: 4, sh: 5, ts: at(12, 0)},
        {i: 5, q: 5, sh: 5, qf: ["blurry"], ts: at(13, 0)},
        {i: 6, q: 1, sh: 1, ts: at(14, 0)}
    ];
    var get = lookup(records);
    // The three best, shown in the order taken.
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4, 5, 6], get, 3), [1, 2, 3]);
    // A blurry picture still beats a dull one, after the sharp ones.
    assert.deepStrictEqual(TRIPS.bestOf([4, 5, 6], get, 2), [4, 5]);
    assert.ok(TRIPS.compareRank(records[1], records[0]) < 0, "a favorite first");
    assert.ok(TRIPS.quality(records[4]) < TRIPS.quality(records[3]), "blurry costs");
    assert.deepStrictEqual(TRIPS.bestOf([], get, 3), []);
    assert.deepStrictEqual(TRIPS.bestOf([99], get, 3), []);
});

test("a large picture is not better for its size alone", function () {
    var records = [
        {i: 1, q: 4, sh: 4, w: 4000, hgt: 3000, ts: at(9, 0)},
        {i: 2, q: 4, sh: 4, w: 11904, hgt: 5952, p: "360", ts: at(12, 0)},
        {i: 3, q: 4, sh: 4, w: 2000, hgt: 1500, ts: at(15, 0)}
    ];
    // (Up to 12 megapixels the larger first; above, all the same: then by id.)
    assert.ok(TRIPS.compareRank(records[0], records[2]) < 0);
    assert.ok(TRIPS.compareRank(records[0], records[1]) < 0);
    // A cover leaves a 360 photo or a wide panorama to the end.
    var cover = [{i: 7, q: 5, sh: 5, p: "360", w: 11904, hgt: 5952, ts: at(9, 0)},
                 {i: 8, q: 4, sh: 4, w: 9000, hgt: 2000, ts: at(12, 0)},
                 {i: 9, q: 3, sh: 3, w: 4000, hgt: 3000, ts: at(15, 0)}];
    assert.deepStrictEqual(TRIPS.bestOf([7, 8, 9], lookup(cover), 1, {perDay: true}), [9]);
    assert.deepStrictEqual(TRIPS.bestOf([7, 8, 9], lookup(cover), 1), [7]);
});

test("one picture of each stack and of each drive", function () {
    var records = [
        {i: 1, q: 5, sh: 4, dg: 2, ts: at(9, 0)},
        {i: 2, q: 5, sh: 5, dg: 2, ts: at(9, 0)},
        {i: 3, q: 3, ts: at(12, 0)},
        // A drive of five clips: its middle one shows, once.
        {i: 10, mv: 1, ty: "video", dv: 10, ts: at(14, 0), dvt: "Drive"},
        {i: 11, mv: 1, ty: "video", dv: 10, ts: at(14, 3), dx: 1},
        {i: 12, mv: 1, ty: "video", dv: 10, ts: at(14, 6), dx: 2},
        {i: 13, mv: 1, ty: "video", dv: 10, ts: at(14, 9), dx: 3},
        {i: 14, mv: 1, ty: "video", dv: 10, ts: at(14, 12), dx: 4}
    ];
    var get = lookup(records);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 10, 11, 12, 13, 14], get, 6), [2, 3, 12]);
    var found = TRIPS.facts([1, 2, 3, 10, 11, 12, 13, 14, 99], get);
    assert.deepStrictEqual(found, {pictures: 3, videos: 0, drives: 1, panoramas: 0});
    assert.strictEqual(TRIPS.factsWords({pictures: 1, videos: 2, drives: 1, panoramas: 3}),
                       "1 picture \u00b7 2 videos \u00b7 1 drive \u00b7 3 in 360\u00b0");
});

test("pictures that look alike are one: the day's look units, joined with stacks", function () {
    // The same view taken again over the day (a hotel window), a stack, a video of
    // the view, and other things.
    var records = [
        {i: 1, q: 4, sh: 4, ts: at(8, 0)},
        {i: 2, q: 5, sh: 4, ts: at(10, 0)},
        {i: 3, q: 4, sh: 3, dg: 3, ts: at(12, 0)},
        {i: 4, q: 3, sh: 3, dg: 3, ts: at(12, 0)},
        {i: 5, q: 3, mv: 1, ty: "video", ts: at(14, 0)},
        {i: 6, q: 2, ts: at(16, 0)},
        {i: 7, q: 2, ts: at(18, 0)},
        {i: 8, mv: 1, ty: "video", dv: 8, ts: at(19, 0)}
    ];
    var get = lookup(records);
    var ids = [1, 2, 3, 4, 5, 6, 7, 8];
    // Without the looks every view shows.
    assert.deepStrictEqual(TRIPS.bestOf(ids, get, 6), [1, 2, 3, 5, 6, 7]);
    // The look units: 1, 2, 4 (and through 4 its stack, 3) and the video 5 are one
    // view; a drive's clip stays its drive's.
    var looks = [[1, 2, 4, 5], [6, 8]];
    var keys = TRIPS.unitKeys(ids, get, looks);
    assert.strictEqual(new Set([1, 2, 3, 4, 5].map(function (id) {
        return keys[id];
    })).size, 1);
    assert.strictEqual(keys[6] === keys[8], false);
    assert.deepStrictEqual(TRIPS.bestOf(ids, get, 6, {looks: looks}), [2, 6, 7, 8]);
    // A trip's cover: the units of all its days.
    var trip = {days: [{date: "2024-05-11", ids: [1, 2], looks: [[1, 2]]},
                       {date: "2024-05-12", ids: [3], looks: []}],
                other: [4, 5], other_looks: [[4, 5]]};
    assert.deepStrictEqual(TRIPS.tripLooks(trip), [[1, 2], [4, 5]]);
});

test("spread over the day: a picture close to one chosen waits its turn", function () {
    var records = [
        {i: 1, q: 5, sh: 5, ts: at(9, 0)},
        {i: 2, q: 5, sh: 4, ts: at(9, 1)},
        {i: 3, q: 5, sh: 3, ts: at(9, 1)},
        {i: 4, q: 2, ts: at(16, 0)}
    ];
    var get = lookup(records);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4], get, 2), [1, 4]);
    // With room for more, the waiting ones come back (best first), but never one of
    // the same moment as one chosen.
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4], get, 3), [1, 2, 4]);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3, 4], get, 4), [1, 2, 4]);
    var moment = lookup([{i: 1, q: 5, ts: at(9, 0)}, {i: 2, q: 4, ts: "2024-05-11T09:00:10"}]);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2], moment, 6), [1]);
});

test("a trip's cover: a picture a day, unless another day's are clearly worse", function () {
    var days = [{i: 5, q: 5, ts: "2024-05-10T09:00:00", dk: "2024-05-10"},
                {i: 6, q: 5, ts: "2024-05-10T15:00:00", dk: "2024-05-10"},
                {i: 7, q: 4, sh: 5, ts: "2024-05-11T09:00:00", dk: "2024-05-11"},
                {i: 8, q: 4, sh: 2, ts: "2024-05-10T18:00:00", dk: "2024-05-10"},
                {i: 9, q: 2, ts: "2024-05-12T09:00:00", dk: "2024-05-12"}];
    var get = lookup(days);
    // The best of the 10th, then the 11th's (half a point worse) before the 10th's
    // second; the 12th's (three points worse) only when nothing else is left.
    assert.deepStrictEqual(TRIPS.bestOf([5, 6, 7, 8, 9], get, 2, {perDay: true}), [5, 7]);
    assert.deepStrictEqual(TRIPS.bestOf([5, 6, 7, 8, 9], get, 3, {perDay: true}), [5, 6, 7]);
    assert.deepStrictEqual(TRIPS.bestOf([5, 6, 7, 8, 9], get, 5, {perDay: true}),
                           [5, 6, 8, 7, 9]);
    assert.deepStrictEqual(TRIPS.bestOf([5, 6, 7], get, 2), [5, 6]);
    // A favorite of a day shown already goes before a better picture of another day.
    var favorite = lookup([{i: 1, q: 5, fav: 1, dk: "2024-05-10", ts: "2024-05-10T09:00:00"},
                           {i: 2, q: 2, fav: 1, dk: "2024-05-10", ts: "2024-05-10T12:00:00"},
                           {i: 3, q: 5, dk: "2024-05-11", ts: "2024-05-11T09:00:00"}]);
    assert.deepStrictEqual(TRIPS.bestOf([1, 2, 3], favorite, 2, {perDay: true}), [1, 2]);
});

test("the searches of a trip and of a day: exactly their pictures", function () {
    function encode(ids) {
        return ids.join(".");
    }
    function tooLong() {
        return "";
    }
    var trip = {title: "Lakes", kind: "library", libraries: ["lib_t", "lib_p"],
                start: "2024-05-10", end: "2024-05-12"};
    assert.deepStrictEqual(TRIPS.tripSearch(trip, [1, 2, 3], encode),
                           {ids: "1.2.3", pick: "Trip: Lakes", sort: "oldest"});
    // Too many for an address: a trip library alone (its pictures whatever their
    // dates); an away trip's libraries on its dates.
    assert.deepStrictEqual(TRIPS.tripSearch(trip, [1, 2, 3], tooLong),
                           {filters: {library: ["lib_t"]}, sort: "oldest"});
    var away = {title: "Farville", kind: "away", libraries: ["lib_a"], start: "2024-03-03",
                end: "2024-03-06"};
    assert.deepStrictEqual(TRIPS.tripSearch(away, [1], tooLong),
                           {filters: {library: ["lib_a"]}, sort: "oldest", from: "2024-03-03",
                            to: "2024-03-06"});
    assert.deepStrictEqual(TRIPS.mapSearch(away), TRIPS.tripSearch(away, [], encode));
    var get = lookup([{i: 1, l: "lib_b"}, {i: 2, l: "lib_t"}, {i: 3, l: "lib_c"}]);
    var day = {date: "2024-05-11", ids: [1, 2, 3, 4]};
    assert.deepStrictEqual(TRIPS.daySearch(trip, day, 2, [1, 2], get, encode),
                           {ids: "1.2", pick: "Trip: Lakes, day 2", sort: "oldest"});
    assert.deepStrictEqual(TRIPS.daySearch(trip, day, 2, [1, 2], get, tooLong),
                           {filters: {library: ["lib_t", "lib_b", "lib_c"]}, from: "2024-05-11",
                            to: "2024-05-11", sort: "oldest"});
    trip.other = [2, 3];
    assert.deepStrictEqual(TRIPS.otherSearch(trip, [2, 3], get, encode),
                           {ids: "2.3", pick: "Trip: Lakes, other pictures", sort: "oldest"});
    assert.deepStrictEqual(TRIPS.otherSearch(trip, [2, 3], get, tooLong),
                           {filters: {library: ["lib_t", "lib_c"]}, sort: "oldest"});
});

test("the search page reads a trip's pictures with what to call them", function () {
    var file = require.resolve(path.join(__dirname, "..", "..", "static", "site.js"));
    delete require.cache[file];
    global.window = {APP_CONFIG: {mode: "server", page: "search", api: "/api",
                                  capabilities: {admin: false}},
                     location: {search: "", protocol: "http:"}};
    var site;
    try {
        site = require(file);
    } finally {
        delete global.window;
    }
    var url = site.searchUrl({ids: "1-2.5", pick: "Trip: Lakes, day 2", sort: "oldest"});
    assert.strictEqual(url, "/?sort=oldest&ids=1-2.5&pick=Trip%3A+Lakes%2C+day+2");
    var state = site.searchState.read(url.slice(1));
    assert.strictEqual(state.ids, "1-2.5");
    assert.strictEqual(state.pick, "Trip: Lakes, day 2");
    // No pick without a list of pictures.
    assert.strictEqual(site.searchState.read("?pick=Hello").pick, "");
    assert.strictEqual(site.searchUrl({pick: "Hello"}), "/");
    assert.strictEqual(site.searchState.read("?ids=1&pick=" + "x".repeat(400)).pick.length, 160);
    assert.strictEqual(site.tripsUrl(), "/trips");
    // Trips turned off: no link to them.
    delete require.cache[file];
    global.window = {APP_CONFIG: {mode: "server", page: "search", api: "/api",
                                  capabilities: {admin: false, trips: false}},
                     location: {search: "", protocol: "http:"}};
    try {
        site = require(file);
    } finally {
        delete global.window;
    }
    assert.strictEqual(site.tripsUrl(), null);
});

test("the map's points: the trip's, fitted without a stray far away", function () {
    var trip = {days: [{date: "2024-05-11", ids: [1, 2, 3, 4, 5]}], other: [6]};
    var locations = [{id: 1, lat: 10.1, lon: 30.1}, {id: 2, lat: 10.2, lon: 30.2},
                     {id: 3, lat: 10.15, lon: 30.05, e: 1}, {id: 6, lat: 10.3, lon: 30.3},
                     {id: 9, lat: 0, lon: 0}];
    var points = TRIPS.tripPoints(trip, locations);
    assert.deepStrictEqual(points.map(function (point) {
        return point.id;
    }), [1, 2, 3, 6]);
    assert.deepStrictEqual(TRIPS.tripIds(trip), [1, 2, 3, 4, 5, 6]);
    var many = [];
    for (var k = 0; k < 40; k++) {
        many.push({id: k, lat: 10.1 + k * 0.001, lon: 30.1});
    }
    many.push({id: 99, lat: -20.0, lon: 60.0});
    assert.strictEqual(TRIPS.fitPoints(many).length, 40);
    // More of them far away: part of the trip (a flight home by another city).
    for (k = 0; k < 6; k++) {
        many.push({id: 100 + k, lat: 5.0, lon: 45.0});
    }
    assert.strictEqual(TRIPS.fitPoints(many).length, many.length);
    // A road trip keeps its ends.
    var road = [];
    for (k = 0; k < 20; k++) {
        road.push({id: k, lat: 1 + k * 0.1, lon: 20});
    }
    assert.strictEqual(TRIPS.fitPoints(road).length, 20);
    assert.strictEqual(TRIPS.fitPoints(road.slice(0, 3)).length, 3);
});

test("an export's pictures have no times: their day's gaps spread them", function () {
    // Six pictures of one day in the order taken: the first three within
    // seconds, then one within two minutes, then two far apart.
    var day = {date: "2024-05-11", ids: ["a", "b", "c", "d", "e", "f"], gaps: "00122"};
    var moments = TRIPS.dayMoments(day);
    var start = Date.UTC(2024, 4, 11) / 1000;
    assert.deepStrictEqual(moments, {a: start, b: start + 1, c: start + 2,
                                     d: start + 2 + TRIPS.SAME_MOMENT_SECONDS,
                                     e: start + 3 + TRIPS.SAME_MOMENT_SECONDS +
                                         TRIPS.SPREAD_SECONDS,
                                     f: start + 4 + TRIPS.SAME_MOMENT_SECONDS +
                                         2 * TRIPS.SPREAD_SECONDS});
    assert.deepStrictEqual(TRIPS.dayMoments({date: "2024-05-11", ids: ["a"]}), {});
    var records = day.ids.map(function (id, index) {
        return {i: id, dk: day.date, ty: "photo", q: 5 - (index % 2), sh: 3};
    });
    var get = lookup(records);
    // Without the gaps nothing says when they were taken: none is held back.
    TRIPS.useMoments([]);
    assert.strictEqual(TRIPS.moment(get("a")), null);
    assert.strictEqual(TRIPS.bestOf(day.ids, get, 3).length, 3);
    TRIPS.useMoments([{days: [day]}]);
    assert.strictEqual(TRIPS.moment(get("c")), start + 2);
    // The same moment is shown once; the best of each moment first, in the order taken.
    assert.deepStrictEqual(TRIPS.bestOf(day.ids, get, 3), ["a", "e", "f"]);
    assert.deepStrictEqual(TRIPS.bestOf(day.ids, get, 6), ["a", "d", "e", "f"]);
    // A record with its own time goes by it.
    assert.strictEqual(TRIPS.moment({i: "a", ts: "2024-05-11T00:00:10"}), start + 10);
    TRIPS.useMoments([]);
});

test("a trip's export: its trip page first, its search page beside it", function () {
    var file = require.resolve(path.join(__dirname, "..", "..", "static", "site.js"));
    delete require.cache[file];
    global.window = {APP_CONFIG: {mode: "static", data: "data/", page: "trips", trip: "trip",
                                  tripsPage: "index.html", searchPage: "search.html",
                                  capabilities: {admin: false, static: true}},
                     location: {search: "", protocol: "file:"}};
    var site;
    try {
        site = require(file);
    } finally {
        delete global.window;
    }
    assert.strictEqual(site.homeUrl(), "index.html");
    assert.strictEqual(site.searchUrl({}), "search.html");
    assert.strictEqual(site.searchUrl({q: "beach"}, "ab12"), "search.html?q=beach#id=ab12");
    assert.strictEqual(site.tripsUrl(), "index.html");
    assert.strictEqual(site.tripUrl("trip"), "index.html?trip=trip");
    assert.strictEqual(site.detailUrl("ab12"), "detail.html?id=ab12");
});

var failed = 0;
tests.forEach(function (item) {
    try {
        item.body();
        console.log("ok - " + item.name);
    } catch (error) {
        failed += 1;
        console.log("FAIL - " + item.name + "\n" + (error && error.stack || error));
    }
});
console.log((tests.length - failed) + " of " + tests.length + " passed");
process.exit(failed ? 1 : 0);
