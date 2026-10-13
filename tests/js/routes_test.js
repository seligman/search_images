/* Tests for static/routes.js (drive routes: the GPS tracks of videos, see
   tracks.py): decoding a clip's points, the place at a moment of a clip,
   the lines of a route (broken at holes and where an export cut a clip),
   the place on a line nearest the pointer, fitting a route in a small map,
   and the export review's sentence about routes (export-review.js). The
   viewer's small map and the strip's speed are tested in tests/js/
   ui_test.js. Every position here is made up.

   Run by tests/test_tracks.py, or directly: node tests/js/routes_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var STATIC = path.join(__dirname, "..", "..", "static");
var ROUTES = require(path.join(STATIC, "routes.js"));
var REVIEW = require(path.join(STATIC, "export-review.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function near(actual, expected, within, message) {
    assert.ok(Math.abs(actual - expected) <= (within || 1e-9),
              (message || "") + " " + actual + " is not about " + expected);
}

// tracks.encode of [[at, lat, lon, speed or null]] (as the server writes it).
function encode(points) {
    var last = null;
    return JSON.stringify(points.map(function (point) {
        var values = [Math.round(point[0] * 10), Math.round(point[1] * 1e5),
                      Math.round(point[2] * 1e5)];
        var speed = point[3] === null ? -1 : Math.round(point[3]);
        var row = last === null ? values.concat([speed])
            : [values[0] - last[0], values[1] - last[1], values[2] - last[2], speed];
        last = values;
        return row;
    }));
}

var LAT = -41.2;
var LON = -150.3;

// A drive of three one-minute clips east along a made-up road, with a hole
// (a missing minute) before the third.
function route() {
    return {d: 11, l: "lib", c: [
        {i: 11, o: 0, p: encode([[0, LAT, LON, 40], [60, LAT, LON + 0.01, 60]])},
        {i: 12, o: 60, p: encode([[0, LAT, LON + 0.0101, 60], [30, LAT, LON + 0.015, 80],
                                  [59, LAT + 0.004, LON + 0.015, null]])},
        {i: 14, o: 180, p: encode([[2, LAT + 0.01, LON + 0.02, 30], [60, LAT + 0.02,
                                                                     LON + 0.02, 30]])}
    ]};
}

/* ---------- Points --------------------------------------------------------------- */

test("a clip's points are decoded as the server encodes them", function () {
    var points = ROUTES.decode(encode([[0, LAT, LON, 12], [12.5, LAT + 0.00123, LON - 0.00456,
                                                            null]]));
    assert.strictEqual(points.length, 2);
    assert.deepStrictEqual(points[0], [0, LAT, LON, 12]);
    near(points[1][0], 12.5);
    near(points[1][1], LAT + 0.00123, 1e-9);
    near(points[1][2], LON - 0.00456, 1e-9);
    assert.strictEqual(points[1][3], null);
    ["", "x", "[1]", "[[1,2,3]]", "[[1,2,3,\"4\"]]", "[[1,2,3,0.5]]", null].forEach(
        function (bad) {
            assert.deepStrictEqual(ROUTES.decode(bad), [], String(bad));
        });
});

test("routes are kept by clip, without clips that have no points", function () {
    var list = [route(), {d: 30, l: "lib", c: [{i: 30, o: 0, p: "[]"}]}, null, {d: 1}];
    var prepared = ROUTES.prepare(list);
    assert.strictEqual(prepared.routes.length, 1);
    assert.deepStrictEqual(Object.keys(prepared.byClip).sort(), ["11", "12", "14"]);
    assert.strictEqual(prepared.byClip[12], prepared.routes[0]);
    assert.deepStrictEqual(prepared.routes[0].clips.map(function (clip) {
        return [clip.id, clip.offset, clip.points.length, clip.cut];
    }), [[11, 0, 2, false], [12, 60, 3, false], [14, 180, 2, false]]);
    assert.deepStrictEqual(ROUTES.prepare(null), {routes: [], byClip: {}});
});

test("the place at a moment of a clip is between its points", function () {
    var points = ROUTES.prepare([route()]).routes[0].clips[1].points;
    var half = ROUTES.positionAt(points, 15);
    near(half.lat, LAT, 1e-9);
    near(half.lon, LON + 0.0101 + (0.015 - 0.0101) / 2, 1e-9);
    near(half.speed, 70, 1e-9);
    // Before the first point and after the last: those points.
    assert.strictEqual(ROUTES.positionAt(points, -5).lon, points[0][2]);
    assert.strictEqual(ROUTES.positionAt(points, 99).lat, points[2][1]);
    // A speed that is not known on one side: the other's.
    assert.strictEqual(ROUTES.positionAt(points, 45).speed, 80);
    assert.strictEqual(ROUTES.positionAt([], 3), null);
});

/* ---------- Lines ---------------------------------------------------------------- */

test("a route's line breaks at a hole and where clips are not shown", function () {
    var prepared = ROUTES.prepare([route()]).routes[0];
    // 11 and 12 follow each other; 14 comes after a missing minute.
    var lines = ROUTES.lines(prepared, null);
    assert.deepStrictEqual(lines.map(function (line) {
        return line.map(function (point) {
            return point.clip;
        });
    }), [[11, 11, 12, 12, 12], [14, 14]]);
    assert.deepStrictEqual(lines[1].map(function (point) {
        return point.t;
    }), [182, 240]);
    // Only the clips the search found.
    lines = ROUTES.lines(prepared, function (id) {
        return id !== 12;
    });
    assert.deepStrictEqual(lines.map(function (line) {
        return line.length;
    }), [2, 2]);
    assert.deepStrictEqual(ROUTES.lines(prepared, function () {
        return false;
    }), []);
});

test("a clip an export cut has a line and a place only in its pieces", function () {
    var cut = route();
    // (The export left out the middle of clip 12: two pieces, "t": 1.)
    cut.c.splice(1, 1, {i: 12, o: 60, t: 1, p: encode([[0, LAT, LON + 0.0101, 60]])},
                 {i: 12, o: 60, t: 1, p: encode([[59, LAT + 0.004, LON + 0.015, 50]])});
    var prepared = ROUTES.prepare([cut]).routes[0];
    assert.deepStrictEqual(ROUTES.lines(prepared, null).map(function (line) {
        return line.map(function (point) {
            return point.clip;
        });
    }), [[11, 11, 12], [12], [14, 14]]);
    // No place where points were left out; within a piece, its place.
    assert.strictEqual(ROUTES.placeOf(prepared, 12, 30), null);
    near(ROUTES.placeOf(prepared, 12, 0).lon, LON + 0.0101, 1e-9);
    assert.strictEqual(ROUTES.placeOf(prepared, 12, 59).speed, 50);
    // A clip that was not cut: as positionAt, also just outside its points.
    near(ROUTES.placeOf(prepared, 14, 0).lat, LAT + 0.01, 1e-9);
    assert.strictEqual(ROUTES.placeOf(prepared, 99, 0), null);
});

test("a clip has no place where it had no fix: before its first, after its last", function () {
    var prepared = ROUTES.prepare([route()]).routes[0];
    assert.strictEqual(ROUTES.FIX_SLACK, 2);
    // Clip 14's first fix is 2 s in (a second or two is a fix a second).
    near(ROUTES.placeOf(prepared, 14, 0).lat, LAT + 0.01, 1e-9);
    // A cold start: a fix only 12 s in. Before it, no place and no speed.
    var cold = route();
    cold.c[2] = {i: 14, o: 180, p: encode([[12, LAT + 0.01, LON + 0.02, 68],
                                          [60, LAT + 0.02, LON + 0.02, 30]])};
    var late = ROUTES.prepare([cold]).routes[0];
    assert.strictEqual(ROUTES.placeOf(late, 14, 0), null);
    assert.strictEqual(ROUTES.placeOf(late, 14, 9.9), null);
    near(ROUTES.placeOf(late, 14, 10).speed, 68, 1e-9);
    // A garage: the fixes stop 60 s into clip 11 (a longer clip).
    near(ROUTES.placeOf(late, 11, 62).lon, LON + 0.01, 1e-9);
    assert.strictEqual(ROUTES.placeOf(late, 11, 63), null);
    var map = new ROUTES.MiniMap({tileSize: 256, tilesMax: 3});
    map.show(late, 14);
    assert.strictEqual(map.at(14, 3), null);
    assert.strictEqual(map.where(14, 3), null);
    assert.strictEqual(map.at(14, 12), 68);
});

test("the place on a line nearest the pointer says the clip and the moment", function () {
    var prepared = ROUTES.prepare([route()]).routes[0];
    // Screen points: x = metres east / 10 of the route's start, y fixed (a
    // straight east-west road for the first two clips).
    var lines = ROUTES.lines(prepared, null).map(function (line) {
        return line.map(function (point) {
            return {sx: (point.lon - LON) * 10000, sy: (LAT - point.lat) * 10000,
                    clip: point.clip, at: point.at};
        });
    });
    var hit = ROUTES.nearest(lines, 50, 3, 6);
    assert.strictEqual(hit.clip, 11);
    near(hit.at, 30, 1e-9);                 // half way along clip 11's line
    near(hit.distance, 3, 1e-9);
    assert.strictEqual(ROUTES.nearest(lines, 50, 30, 6), null);
    // Between two clips' points: the moment of the nearer end.
    var join = ROUTES.nearest(lines, 100.9, 0, 6);
    assert.strictEqual(join.clip, 12);
    assert.strictEqual(join.at, 0);
    // A one-point line is found too.
    var alone = ROUTES.nearest([[{sx: 5, sy: 5, clip: 7, at: 3}]], 7, 5, 6);
    assert.deepStrictEqual([alone.clip, alone.at, alone.distance], [7, 3, 2]);
});

test("a small map fits the route", function () {
    var box = {south: -41.3, north: -41.2, west: -150.4, east: -150.2};
    var zoom = ROUTES.fitZoom(box, 300, 170, 256, 14, 16);
    // The box's 0.2 degrees of longitude take at most 272 of the 300 pixels.
    var world = 256 * Math.pow(2, zoom + 1);
    assert.ok(0.2 / 360 * world <= 272 + 1e-6);
    assert.ok(0.1 / 180 * world / 2 <= 142 + 1e-6);
    near(Math.max(0.2 / 360 * world / 272, 0.1 / 180 * world / 2 / 142), 1, 1e-9);
    // A car that stood still: as far in as allowed.
    assert.strictEqual(ROUTES.fitZoom({south: 1, north: 1, west: 2, east: 2}, 300, 170, 256,
                                      14, 16), 16);
    var bounds = ROUTES.boxOf(ROUTES.prepare([route()]).routes[0]);
    near(bounds.north, LAT + 0.02, 1e-9);
    near(bounds.east, LON + 0.02, 1e-9);
});

test("the small map says the speed where the clip that plays is", function () {
    var map = new ROUTES.MiniMap({tileSize: 256, tilesMax: 3});
    assert.strictEqual(map.node, null);       // (no page under Node)
    map.show(ROUTES.prepare([route()]).routes[0], 11);
    near(map.at(11, 30), 50, 1e-9);
    assert.strictEqual(map.at(12, 59), 80);   // (a point without one: the one before)
    assert.strictEqual(map.at(13, 0), null);  // (not a clip of the route)
    assert.strictEqual(map.clipId, 13);
    map.hide();
    assert.strictEqual(map.at(11, 30), null);
});

/* ---------- The export review ---------------------------------------------------- */

test("the export review says what goes of the routes", function () {
    assert.strictEqual(REVIEW.routesText({}), "");
    assert.strictEqual(REVIEW.routesText({routes: {on: false, positions: 0, withheld: 0,
                                                   routes: 0}}), "");
    assert.strictEqual(REVIEW.routesText({routes: {on: false, positions: 5, withheld: 5,
                                                   routes: 0}}),
                       "5 clips keep their place names but not the position from their GPS " +
                       "track (Drive routes is off).");
    assert.strictEqual(REVIEW.routesText({routes: {on: true, positions: 5, withheld: 2,
                                                   routes: 1}}),
                       "1 drive route is published; 2 clips near where a drive starts or ends " +
                       "keep only their place names.");
    assert.strictEqual(REVIEW.routesText({routes: {on: true, positions: 3, withheld: 0,
                                                   routes: 2}}),
                       "2 drive routes are published.");
    assert.ok(/drive route/.test(REVIEW.detailsText({location: true, routes: true})));
    assert.ok(!/drive route/.test(REVIEW.detailsText({location: false, routes: true})));
    assert.ok(REVIEW.OPTIONS.routes.label && REVIEW.OPTIONS.routes_trim.label);
});

test("the export review warns that a dashcam's pictures show its position", function () {
    assert.strictEqual(REVIEW.overlayText({}), "");
    assert.strictEqual(REVIEW.overlayText({routes: {overlay: 0, near: 0}}), "");
    assert.strictEqual(REVIEW.overlayText({routes: {overlay: 7, near: 0}}),
                       "7 dashcam clips are published (as videos or poster frames). A dashcam " +
                       "prints its GPS position over its video, so their pictures show it " +
                       "whatever Location and Drive routes say (the positions in their texts " +
                       "are left out).");
    var text = REVIEW.overlayText({routes: {overlay: 1, near: 1}});
    assert.ok(/^1 dashcam clip is published \(as a video or its poster frame\)/.test(text));
    assert.ok(/1 of them was filmed near where a drive starts or ends, or without a GPS fix/
              .test(text));
    assert.ok(/hide it to keep that place private\.$/.test(text));
    assert.ok(/hide them to keep/.test(REVIEW.overlayText({routes: {overlay: 9, near: 6}})));
});

/* ---------- Run ------------------------------------------------------------------- */

var failed = 0;
tests.forEach(function (entry) {
    try {
        entry.body();
        console.log("ok   " + entry.name);
    } catch (error) {
        failed += 1;
        console.log("FAIL " + entry.name + "\n     " + (error && error.stack || error));
    }
});
console.log((tests.length - failed) + " of " + tests.length + " passed");
process.exit(failed ? 1 : 0);
