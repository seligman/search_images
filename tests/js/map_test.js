/* Tests for the parts of static/map.js that need no page: the projection
   and the repeating world, grouping pictures into pins (also across the
   date line, and the same wherever the map is panned to), the pin under
   the pointer, the wheel, and fitting the view to the pictures; and of the
   map page (the real scripts on the made-up DOM of app_meaning_test.js,
   with a fake fetch() for the server's answers): a search that names a
   person fits their pictures once the people are in.

   Run by tests/test_ui_scripts.py, or directly: node tests/js/map_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var MAP = require(path.join(__dirname, "..", "..", "static", "map.js"));

var TILE = 256;
var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function near(actual, expected, within, message) {
    assert.ok(Math.abs(actual - expected) <= (within || 1e-9),
              (message || "") + " " + actual + " is not about " + expected);
}

// The screen x of a place in a view (centre lat/lon, zoom, screen width).
function screenXs(lon, view, screenW) {
    var width = MAP.worldWidth(TILE, view.zoom);
    var centre = MAP.project(view.lat, view.lon, width);
    var at = MAP.project(0, lon, width);
    return MAP.copies(at.x, centre.x - screenW / 2, screenW, width, 0);
}

/* ---------- Projection and the repeating world ---------------------------- */

test("the world is square (Web Mercator) and grows twice per zoom", function () {
    assert.strictEqual(MAP.worldWidth(TILE, 0), 512);
    assert.strictEqual(MAP.worldWidth(TILE, 3), 4096);
    var width = MAP.worldWidth(TILE, 2);
    near(MAP.project(MAP.MAX_LAT, -180, width).x, 0);
    near(MAP.project(MAP.MAX_LAT, -180, width).y, 0, 1e-6);
    assert.deepStrictEqual(MAP.project(0, 0, width), {x: 1024, y: 1024});
    near(MAP.project(-MAP.MAX_LAT, 90, width).x, 1536);
    near(MAP.project(-MAP.MAX_LAT, 90, width).y, 2048, 1e-6);
    // The poles are left out: further north (or south) is on the edge.
    assert.strictEqual(MAP.project(89.9, 10, width).y, MAP.project(MAP.MAX_LAT, 10, width).y);
    assert.strictEqual(MAP.project(-90, 10, width).y, MAP.project(-MAP.MAX_LAT, 10, width).y);
    // A degree of latitude is as tall as a degree of longitude is wide at
    // the equator, and twice as tall at 60 degrees.
    var equator = MAP.project(-0.5, 0, width).y - MAP.project(0.5, 0, width).y;
    near(equator, width / 360, 1e-3);
    var sixty = MAP.project(59.5, 0, width).y - MAP.project(60.5, 0, width).y;
    near(sixty / equator, 2, 2e-3);
    // lon 180 is the date line again: x 0, not x = width.
    assert.strictEqual(MAP.project(10, 180, width).x, 0);
    [[51.5, -0.12], [-33.86, 151.2], [64.1, -21.9], [-17.7, 179.99], [0, -180]].forEach(function (p) {
        var at = MAP.project(p[0], p[1], width);
        var back = MAP.unproject(at.x, at.y, width);
        near(back.lat, p[0], 1e-9);
        near(MAP.normalizeLon(back.lon), MAP.normalizeLon(p[1]), 1e-9);
    });
});

test("x and longitudes wrap around", function () {
    assert.strictEqual(MAP.wrapX(-1, 512), 511);
    assert.strictEqual(MAP.wrapX(512, 512), 0);
    assert.strictEqual(MAP.wrapX(1300, 512), 276);
    assert.strictEqual(MAP.normalizeLon(180), -180);
    assert.strictEqual(MAP.normalizeLon(190), -170);
    assert.strictEqual(MAP.normalizeLon(-540), -180);
    assert.strictEqual(MAP.nearestCopy(10, 500, 512), 522);
    assert.strictEqual(MAP.nearestCopy(500, 10, 512), -12);
});

test("a picture near the date line shows when the map is centred across it", function () {
    // The old map lost this pin: centred on lon -179.5 at zoom 5, a
    // picture at +179.5 is 1 degree (about 45 px) west of the centre.
    var view = {lat: -17, lon: -179.5, zoom: 5};
    var xs = screenXs(179.5, view, 1200);
    assert.strictEqual(xs.length, 1);
    near(xs[0], 600 - MAP.worldWidth(TILE, 5) / 360, 1e-6);
    // And the other way round.
    var back = screenXs(-179.5, {lat: -17, lon: 179.5, zoom: 5}, 1200);
    assert.strictEqual(back.length, 1);
    near(back[0], 600 + MAP.worldWidth(TILE, 5) / 360, 1e-6);
});

test("on a screen wider than the world every copy of the world has its pins", function () {
    // Zoom 1: the world is 1024 px; a 1920 px screen shows it about twice.
    var tokyo = screenXs(139.7, {lat: 20, lon: 0, zoom: 1}, 1920);
    assert.strictEqual(tokyo.length, 2);
    near(tokyo[1] - tokyo[0], 1024, 1e-9);
    var sf = screenXs(-122.4, {lat: 20, lon: 0, zoom: 1}, 1920);
    assert.strictEqual(sf.length, 2);
    // Zoom 0 on a 2560 px screen: five copies (and parts).
    assert.ok(screenXs(0, {lat: 0, lon: 0, zoom: 0}, 2560).length >= 5);
    assert.deepStrictEqual(MAP.copies(10, 0, 100, 0, 0), []);
});

test("copies reach just past the screen for pins that stick into it", function () {
    assert.deepStrictEqual(MAP.copies(1000, 0, 990, 1024, 0), []);
    assert.deepStrictEqual(MAP.copies(1000, 0, 990, 1024, 12), [1000]);
    assert.deepStrictEqual(MAP.copies(1020, 0, 100, 1024, 6), [-4]);
});

/* ---------- Pins -------------------------------------------------------------- */

function points(list) {
    return list.map(function (item, index) {
        return {id: item[2] !== undefined ? item[2] : index + 1, lat: item[0], lon: item[1]};
    });
}

function idsOf(groups) {
    return groups.map(function (group) {
        return group.ids.slice().sort(function (a, b) {
            return a - b;
        }).join(",");
    }).sort();
}

function assertNoOverlap(groups, width) {
    for (var a = 0; a < groups.length; a++) {
        for (var b = a + 1; b < groups.length; b++) {
            var dx = MAP.nearestCopy(groups[b].x, groups[a].x, width) - groups[a].x;
            var dy = groups[b].y - groups[a].y;
            var gap = Math.sqrt(dx * dx + dy * dy) - groups[a].r - groups[b].r;
            assert.ok(gap >= MAP.GAP - 1e-9, "pins " + a + " and " + b + " overlap: " + gap);
        }
    }
}

test("pictures on both sides of the date line make one pin", function () {
    var list = points([[-17.0, 179.9], [-17.05, -179.9], [-16.95, 179.95], [35.7, 139.7]]);
    for (var zoom = 0; zoom <= 6; zoom++) {
        var width = MAP.worldWidth(TILE, zoom);
        var groups = MAP.cluster(list, width);
        assert.deepStrictEqual(idsOf(groups), ["1,2,3", "4"], "zoom " + zoom);
        var seam = groups.filter(function (group) {
            return group.count === 3;
        })[0];
        // Its pin sits on the date line (x near 0 or near width), not mid-world.
        var fromSeam = Math.min(seam.x, width - seam.x);
        assert.ok(fromSeam < 0.2 / 360 * width + 1, "zoom " + zoom + ": x " + seam.x);
        assertNoOverlap(groups, width);
    }
    // Zoomed in far enough, the two sides come apart.
    var far = MAP.cluster(list, MAP.worldWidth(TILE, 10));
    assert.ok(far.length >= 3, JSON.stringify(idsOf(far)));
});

test("pins never overlap, and nearby pictures share one", function () {
    // A dense town, a few villages, scattered single pictures.
    var list = [];
    var seed = 7;
    function random() {
        seed = (seed * 16807) % 2147483647;
        return seed / 2147483647;
    }
    for (var k = 0; k < 600; k++) {
        list.push({id: k, lat: 48.85 + (random() - 0.5) * 0.2, lon: 2.35 + (random() - 0.5) * 0.3});
    }
    for (var v = 0; v < 200; v++) {
        list.push({id: 1000 + v, lat: 40 + random() * 15, lon: -5 + random() * 20});
    }
    for (var s = 0; s < 100; s++) {
        list.push({id: 2000 + s, lat: random() * 140 - 70, lon: random() * 360 - 180});
    }
    for (var zoom = 0; zoom <= 9; zoom++) {
        var width = MAP.worldWidth(TILE, zoom);
        var groups = MAP.cluster(list, width);
        assertNoOverlap(groups, width);
        var total = 0;
        groups.forEach(function (group) {
            total += group.count;
            assert.strictEqual(group.ids.length, group.count);
            assert.strictEqual(group.r, MAP.pinRadius(group.count));
            assert.ok(group.x >= 0 && group.x < width);
        });
        assert.strictEqual(total, list.length, "zoom " + zoom);
    }
    // Two pictures 4 px apart, right across a cell edge: still one pin.
    var width2 = MAP.worldWidth(TILE, 3);
    var edge = 3 * MAP.CELL;
    var a = MAP.unproject(edge - 2, 700, width2);
    var b = MAP.unproject(edge + 2, 700, width2);
    assert.deepStrictEqual(idsOf(MAP.cluster(points([[a.lat, a.lon], [b.lat, b.lon]]), width2)),
                           ["1,2"]);
});

test("the pins stay the same while the map is panned", function () {
    var list = [];
    for (var k = 0; k < 300; k++) {
        list.push({id: k, lat: 45 + (k % 17) * 0.031 + Math.floor(k / 17) * 0.002,
                   lon: 7 + (k % 23) * 0.043 - Math.floor(k / 23) * 0.003});
    }
    var zoom = 9;
    var width = MAP.worldWidth(TILE, zoom);
    var groups = MAP.cluster(list, width);
    var again = MAP.cluster(list.slice().reverse(), width);
    assert.deepStrictEqual(idsOf(again), idsOf(groups));
    // Every pan offset (1 px steps, as a drag makes) shows the same pins
    // in the same places of the world (the old map grouped by cells of the
    // screen: a 1 px drag could split or merge pins).
    var centre = MAP.project(45.25, 7.5, width);
    var reference = null;
    function inRegion(pin, left, top) {
        var x = pin.sx + left - centre.x;
        var y = pin.sy + top - centre.y;
        return x > -300 && x < 300 && y > -200 && y < 200;
    }
    for (var dx = 0; dx < 80; dx++) {
        var left = centre.x - 600 + dx;
        var top = centre.y - 400 + dx / 2;
        var shown = MAP.screenPins(groups, left, top, 1200, 800, width).filter(function (pin) {
            return inRegion(pin, left, top);
        });
        var key = idsOf(shown.map(function (pin) {
            return pin.group;
        }));
        if (reference === null) {
            reference = key;
            assert.ok(reference.length > 3, "pins in the middle: " + reference.length);
        }
        assert.deepStrictEqual(key, reference, "panned by " + dx);
    }
});

test("pin sizes follow the number of pictures", function () {
    assert.strictEqual(MAP.pinRadius(1), 7);
    assert.ok(MAP.pinRadius(2) > MAP.pinRadius(1));
    assert.ok(MAP.pinRadius(50) >= MAP.pinRadius(10));
    assert.strictEqual(MAP.pinRadius(100000), MAP.MAX_RADIUS);
    // Room for "1,234" in a pin of a thousand pictures.
    assert.ok(MAP.pinRadius(1000) * 2 >= 36);
    assert.strictEqual(MAP.countLabel(7), "7");
    assert.strictEqual(MAP.countLabel(1234), "1,234");
    assert.strictEqual(MAP.countLabel(1005), "1,005");
    assert.strictEqual(MAP.countLabel(23456), "23k");
});

test("points without a valid place are left out", function () {
    var groups = MAP.cluster([{id: 1, lat: 95, lon: 0}, {id: 2, lat: 0, lon: NaN},
                              {id: 3, lat: "1", lon: 2}, null, {id: 4, lat: 10, lon: 20}],
                             MAP.worldWidth(TILE, 2));
    assert.deepStrictEqual(idsOf(groups), ["4"]);
    assert.deepStrictEqual(MAP.cluster([], 512), []);
});

test("the pin under the pointer is the nearest, and on a tie the top one", function () {
    var pins = [{sx: 100, sy: 100, r: 10, name: "a"}, {sx: 125, sy: 100, r: 10, name: "b"},
                {sx: 300, sy: 300, r: 7, name: "c"}];
    assert.strictEqual(MAP.hitPin(pins, 103, 100, 4).name, "a");
    assert.strictEqual(MAP.hitPin(pins, 121, 100, 4).name, "b");
    // Exactly between a and b (both edges 2.5 px away): b, drawn last.
    assert.strictEqual(MAP.hitPin(pins, 112.5, 100, 4).name, "b");
    assert.strictEqual(MAP.hitPin(pins, 309, 300, 4).name, "c");
    assert.strictEqual(MAP.hitPin(pins, 312, 300, 4), null);
    assert.strictEqual(MAP.hitPin(pins, 312, 300, 12).name, "c");
    assert.strictEqual(MAP.hitPin([], 1, 1, 4), null);
});

/* ---------- The wheel ------------------------------------------------------------ */

test("one notch of a mouse wheel is one zoom level", function () {
    var wheel = new MAP.WheelZoom();
    assert.deepStrictEqual(wheel.feed({deltaY: -100, timeStamp: 0}), {steps: 1, panX: 0});
    assert.deepStrictEqual(wheel.feed({deltaY: -100, timeStamp: 50}), {steps: 1, panX: 0});
    assert.deepStrictEqual(wheel.feed({deltaY: 100, timeStamp: 1000}), {steps: -1, panX: 0});
    // Larger notches (two of 120 at once) do not jump two levels at once.
    assert.deepStrictEqual(wheel.feed({deltaY: 240, timeStamp: 2000}), {steps: -1, panX: 0});
    assert.deepStrictEqual(wheel.feed({deltaY: 120, timeStamp: 2050}), {steps: -1, panX: 0});
    // Lines (Firefox) and pages count too.
    assert.strictEqual(new MAP.WheelZoom().feed({deltaY: 3, deltaMode: 1, timeStamp: 0}).steps, -1);
    assert.strictEqual(new MAP.WheelZoom().feed({deltaY: -1, deltaMode: 2, timeStamp: 0}).steps, 1);
});

// The zoom steps of a gentle, steady two-finger scroll: px pixels, 8 at a
// time, 60 times a second.
function gentleScroll(px) {
    var wheel = new MAP.WheelZoom();
    var steps = 0;
    for (var k = 0; k < Math.round(px / 8); k++) {
        steps += wheel.feed({deltaY: -8, timeStamp: k * 16}).steps;
    }
    return steps;
}

test("a touchpad's small steps add up instead of each zooming", function () {
    // A slight scroll does not zoom; a long one zooms a level or two, not
    // a level per 100 px (the first map went from zoom 2 to 9 on 800 px).
    assert.strictEqual(gentleScroll(150), 0);
    assert.strictEqual(gentleScroll(320), 1);
    assert.strictEqual(gentleScroll(800), 2);
    var wheel;
    // A pause starts over: what was left is not added to the next gesture.
    wheel = new MAP.WheelZoom();
    for (var m = 0; m < 10; m++) {
        assert.strictEqual(wheel.feed({deltaY: -9, timeStamp: m * 16}).steps, 0);
    }
    assert.strictEqual(wheel.feed({deltaY: -9, timeStamp: 2000}).steps, 0);
    // So does turning round.
    wheel = new MAP.WheelZoom();
    wheel.feed({deltaY: -90, timeStamp: 0});
    assert.strictEqual(wheel.feed({deltaY: 20, timeStamp: 10}).steps, 0);
    assert.strictEqual(wheel.feed({deltaY: -20, timeStamp: 20}).steps, 0);
});

// The zoom steps of a flick: count events gap ms apart (default 16: 60 a
// second), the first of first pixels, each next one decay times the one
// before (its momentum).
function flick(first, decay, count, gap) {
    var wheel = new MAP.WheelZoom();
    var steps = 0;
    var total = 0;
    for (var k = 0; k < count; k++) {
        var delta = first * Math.pow(decay, k);
        total += delta;
        steps += wheel.feed({deltaY: -delta, timeStamp: k * (gap || 16)}).steps;
    }
    return {steps: steps, total: total};
}

test("a flick on a touchpad and its momentum zoom a level or two", function () {
    // (The first map went from zoom 2 to 7 on a 611 px flick.)
    var soft = flick(30, 0.95, 60);
    assert.ok(soft.total > 550, String(soft.total));
    assert.strictEqual(soft.steps, 1);
    var strong = flick(45, 0.97, 60);
    assert.ok(strong.total > 1200, String(strong.total));
    assert.ok(strong.steps >= 1 && strong.steps <= 2, String(strong.steps));
    // Even one that starts with steps as large as a wheel's notch.
    var fast = flick(150, 0.95, 60);
    assert.ok(fast.total > 2500, String(fast.total));
    assert.ok(fast.steps >= 1 && fast.steps <= 3, String(fast.steps));
    // And one whose events come further apart (a busy page): steps that
    // change in size are not a mouse's notches.
    var slow = flick(40, 0.95, 60, 62);
    assert.ok(slow.total > 700, String(slow.total));
    assert.ok(slow.steps >= 1 && slow.steps <= 2, String(slow.steps));
    assert.ok(flick(120, 0.9, 30, 40).steps <= 2);
    // The next gesture (after a pause) zooms again.
    var wheel = new MAP.WheelZoom();
    for (var k = 0; k < 40; k++) {
        wheel.feed({deltaY: -10, timeStamp: k * 16});
    }
    var again = 0;
    for (var m = 0; m < 40; m++) {
        again += wheel.feed({deltaY: -10, timeStamp: 2000 + m * 16}).steps;
    }
    assert.strictEqual(again, 1);
    // A mouse wheel turned quickly but notch by notch still zooms a
    // level per notch.
    var mouse = new MAP.WheelZoom();
    var notches = 0;
    for (var n = 0; n < 5; n++) {
        notches += mouse.feed({deltaY: -100, timeStamp: n * 40}).steps;
    }
    assert.strictEqual(notches, 5);
    // (Notches the browser joined into one event are still notches.)
    notches = 0;
    [-100, -200, -100].forEach(function (delta, index) {
        notches += mouse.feed({deltaY: delta, timeStamp: 1000 + index * 40}).steps;
    });
    assert.strictEqual(notches, 3);
});

test("a pinch on a touchpad (the wheel with Ctrl) zooms", function () {
    // Its steps are small (a few pixels): they count more than scrolling.
    var wheel = new MAP.WheelZoom();
    for (var k = 0; k < 5; k++) {
        assert.strictEqual(wheel.feed({deltaY: -6, ctrlKey: true, timeStamp: k * 16}).steps, 0);
    }
    assert.strictEqual(wheel.feed({deltaY: -6, ctrlKey: true, timeStamp: 80}).steps, 1);
    var scroll = new MAP.WheelZoom();
    var steps = 0;
    for (var m = 0; m < 6; m++) {
        steps += scroll.feed({deltaY: -6, timeStamp: m * 16}).steps;
    }
    assert.strictEqual(steps, 0);
});

test("a sideways swipe pans and never zooms", function () {
    var wheel = new MAP.WheelZoom();
    // The old map zoomed out on every event with deltaY 0.
    for (var k = 0; k < 20; k++) {
        var move = wheel.feed({deltaX: 30, deltaY: 0, timeStamp: k * 16});
        assert.strictEqual(move.steps, 0);
        assert.strictEqual(move.panX, -30);
    }
    // Mostly sideways with a little up or down: still a pan.
    var slanted = wheel.feed({deltaX: -40, deltaY: 10, timeStamp: 400});
    assert.deepStrictEqual(slanted, {steps: 0, panX: 40});
    assert.deepStrictEqual(wheel.feed({deltaX: 0, deltaY: 0, timeStamp: 500}), {steps: 0, panX: 0});
    // Lines sideways (a tilt wheel in Firefox).
    assert.strictEqual(wheel.feed({deltaX: 2, deltaY: 0, deltaMode: 1, timeStamp: 600}).panX, -80);
});

/* ---------- Views ------------------------------------------------------------------- */

test("the view fits the pictures, the short way round the world", function () {
    var view = MAP.fitView(points([[-17, 179.9], [-18, -179.9]]), 1400, 800, TILE, 0, 6, 40);
    // (The middle in Web Mercator: a little nearer the pole than the
    // middle latitude.)
    near(view.lat, -17.5, 2e-3);
    assert.ok(view.lat < -17.5, String(view.lat));
    assert.ok(Math.abs(Math.abs(view.lon) - 180) < 1e-9, String(view.lon));
    assert.strictEqual(view.zoom, 6);
    // Europe: the view is around it, not around the whole world.
    var europe = MAP.fitView(points([[48.85, 2.35], [52.52, 13.4], [41.9, 12.5], [38.7, -9.1]]),
                             1400, 800, TILE, 0, 6, 40);
    assert.ok(europe.lon > 0 && europe.lon < 5, String(europe.lon));
    var width5 = MAP.worldWidth(TILE, 5);
    near(MAP.project(europe.lat, 0, width5).y,
         (MAP.project(38.7, 0, width5).y + MAP.project(52.52, 0, width5).y) / 2, 1e-6);
    // 22.5 degrees across would fit 1,320 px at zoom 5 (1,024 px), but its
    // 14 degrees of latitude take 906 px there, more than 720: zoom 4.
    assert.strictEqual(europe.zoom, 4);
    assert.strictEqual(MAP.fitView(points([[48.85, 2.35], [38.7, -9.1], [52.52, 13.4]]),
                                   1400, 800, TILE, 0, 3, 40).zoom, 3);
    // All over the world: zoomed out.
    var wide = MAP.fitView(points([[60, -150], [-40, 170], [0, 20]]), 1400, 800, TILE, 0, 6, 40);
    assert.strictEqual(wide.zoom, 2);       // 190 degrees: 1,081 px at zoom 2
    near(wide.lon, 115, 1e-9);          // from 20 east across the date line to -150
    var world = MAP.fitView(points([[70, -170], [-50, -60], [10, 60], [-30, 150]]),
                            1400, 800, TILE, 0, 6, 40);
    assert.strictEqual(world.zoom, 1);
    // One picture: as close as allowed.
    assert.strictEqual(MAP.fitView(points([[10, 10]]), 800, 600, TILE, 0, 3).zoom, 3);
    assert.strictEqual(MAP.fitView([], 800, 600, TILE, 0, 3), null);
});

test("a view reads back from the address", function () {
    var text = MAP.formatView({lat: 48.856613, lon: 362.3522, zoom: 7});
    assert.strictEqual(text, "48.8566,2.3522,7");
    assert.deepStrictEqual(MAP.parseView(text), {lat: 48.8566, lon: 2.3522, zoom: 7});
    assert.strictEqual(MAP.formatView({lat: -0.00001, lon: -180, zoom: 2.4}), "0,-180,2");
    assert.strictEqual(MAP.parseView("91,0,1"), null);
    assert.strictEqual(MAP.parseView("1,2"), null);
    assert.strictEqual(MAP.parseView(""), null);
    assert.deepStrictEqual(MAP.parseView("0,180,0"), {lat: 0, lon: -180, zoom: 0});
});

test("the open list reads back from the address and finds its pin again", function () {
    var places = points([[48.8566, 2.3522], [48.857, 2.353], [48.86, 2.34], [51.5, -0.12],
                         [-17.0, 179.99], [-17.01, -179.99]]);
    [3, 6, 10, 14].forEach(function (zoom) {
        var width = MAP.worldWidth(TILE, zoom);
        var groups = MAP.cluster(places, width);
        groups.forEach(function (group) {
            var middle = MAP.unproject(group.x, group.y, width);
            var text = MAP.formatPoint(middle);
            var back = MAP.parsePoint(text);
            assert.ok(back, text);
            // The same pin, at every zoom (also the one across the date line).
            assert.strictEqual(MAP.groupAt(groups, back.lat, back.lon, width), group,
                               zoom + " " + text);
        });
    });
    // Nowhere near a pin: none.
    var groups = MAP.cluster(places, MAP.worldWidth(TILE, 5));
    assert.strictEqual(MAP.groupAt(groups, 0, 0, MAP.worldWidth(TILE, 5)), null);
    assert.strictEqual(MAP.formatPoint({lat: -0.0000001, lon: 180, zoom: 1}), "0,-180");
    ["", "1", "91,0", "0,181", "a,b", "1,2,3", "1e3,2"].forEach(function (bad) {
        assert.strictEqual(MAP.parsePoint(bad), null, bad);
    });
});

test("a chip of the viewer opens a new search, without the dates or the area", function () {
    var state = {q: "beach", lib: "lib_family1", filters: {type: ["photo"]}, from: "2019",
                 to: "2019", view: "37.1,-8.2,9", ids: "", group: true,
                 area: "l50.WYro8WaXz1m-1Wz3", aname: "Test Road"};
    var target = MAP.chipState(state, "place", "Portugal|Algarve|Faro");
    assert.deepStrictEqual(target, {q: "", lib: "lib_family1",
                                    filters: {place: ["Portugal|Algarve|Faro"]},
                                    from: "", to: "", view: "", ids: "", group: true,
                                    area: "", aname: ""});
    target = MAP.chipState(state, "q", "tag:sunset");
    assert.strictEqual(target.q, "tag:sunset");
    assert.deepStrictEqual(target.filters, {});
    assert.strictEqual(target.from + target.to, "");
    // The state it was made from is left as it was.
    assert.strictEqual(state.from, "2019");
});

/* ---------- Pins on a canvas (shared with the trip pages; the tiles: streets_test.js) - */

// A canvas context that writes down what is drawn.
function recorder() {
    var calls = [];
    var ctx = {globalAlpha: 1, calls: calls};
    ["drawImage", "fillRect", "beginPath", "arc", "fill", "stroke", "fillText", "save",
     "restore"].forEach(function (name) {
        ctx[name] = function () {
            calls.push([name].concat(Array.prototype.slice.call(arguments)));
        };
    });
    return ctx;
}

function named(ctx, name) {
    return ctx.calls.filter(function (call) {
        return call[0] === name;
    });
}

test("pins: red for one, blue with a count for more, hollow for estimated places", function () {
    var ctx = recorder();
    MAP.drawPins(ctx, [{sx: 10, sy: 10, r: 7, group: {count: 1, estimated: 0}},
                       {sx: 50, sy: 50, r: 12, group: {count: 1234, estimated: 0}},
                       {sx: 90, sy: 90, r: 9, group: {count: 3, estimated: 3, picked: true}}]);
    assert.strictEqual(named(ctx, "fill").length, 3);
    // The picked pin's ring (on a dark edge: seen on light street maps too), the
    // white edges, and the hollow pin's two rings.
    assert.strictEqual(named(ctx, "stroke").length, 6);
    assert.deepStrictEqual(named(ctx, "fillText").map(function (call) {
        return call[1];
    }), ["1,234", "3"]);
    assert.strictEqual(named(ctx, "save").length, 1);
    assert.strictEqual(named(ctx, "restore").length, 1);
});

/* ---------- The map page ---------------------------------------------------------- */

var fs = require("fs");
var vm = require("vm");
var FAKE = require(path.join(__dirname, "app_meaning_test.js"));
var ROOT = path.join(__dirname, "..", "..");
var MAP_PAGE = fs.readFileSync(path.join(ROOT, "templates", "map.html"), "utf8");
var MAP_SCRIPTS = ["site.js", "shell.js", "search-core.js", "streets.js", "routes.js",
                   "viewer.js", "map.js"].map(function (name) {
    return {name: name, code: fs.readFileSync(path.join(ROOT, "static", name), "utf8")};
});

// Made-up pictures: Cleo's two (around 45 N 10 E), and others far away.
var MAP_RECORDS = [
    {i: 1, l: "lib", t: "Harbour", g: ["harbour"], dk: "2024-05-01", ds: 1},
    {i: 2, l: "lib", t: "Canal", g: ["canal"], dk: "2024-05-02", ds: 1},
    {i: 3, l: "lib", t: "Desert", g: ["desert"], dk: "2024-05-03", ds: 1},
    {i: 4, l: "lib", t: "Forest", g: ["forest"], dk: "2024-05-04", ds: 1}
];
var MAP_POINTS = [{id: 1, lat: 45.0, lon: 10.0}, {id: 2, lat: 45.02, lon: 10.03},
                  {id: 3, lat: -30.0, lon: 140.0}, {id: 4, lat: 10.0, lon: -60.0}];
var MAP_PEOPLE = [{id: 1, name: "Cleo", label: "Cleo", cover: null, faces: 2, ids: [1, 2]}];

function mapResponse(body) {
    var text = JSON.stringify(body);
    return Promise.resolve({
        ok: body !== null,
        status: body !== null ? 200 : 404,
        headers: {get: function () {
            return null;
        }},
        json: function () {
            return Promise.resolve(JSON.parse(text));
        }
    });
}

/* The admin's map page at an address (searchText: its ?... part; records:
   the search records, MAP_RECORDS by default; html: the page, MAP_PAGE by
   default; setup(document): called before the scripts run). page.urls: the
   addresses it wrote; page.views() their views ([lat, lon, zoom]). */
function mapPage(searchText, records, html, setup) {
    var urls = [];
    var document = FAKE.makeDocument();
    var config = {mode: "server", page: "map", api: "/api", tiles: "/tile", tilesMax: 8,
                  adminApi: "/api/admin", libraries: ["Example"],
                  libraryList: [{id: "lib", name: "Example"}],
                  capabilities: {admin: true, static: false, people: true}};
    FAKE.parseBody(document, html || MAP_PAGE, config);
    var canvas = document.getElementById("map-canvas");
    canvas.getContext = function () {
        // (A context that draws nothing.)
        return new Proxy({}, {get: function (target, name) {
            if (name === "measureText") {
                return function (text) {
                    return {width: String(text).length * 6};
                };
            }
            if (!(name in target)) {
                target[name] = function () {};
            }
            return target[name];
        }, set: function () {
            return true;
        }});
    };
    canvas.getBoundingClientRect = function () {
        return {top: 0, left: 0, right: 1000, bottom: 700, width: 1000, height: 700};
    };
    if (setup) {
        setup(document);
    }
    function storage() {
        var items = {};
        return {
            getItem: function (key) {
                return Object.prototype.hasOwnProperty.call(items, key) ? items[key] : null;
            },
            setItem: function (key, value) {
                items[key] = String(value);
            },
            removeItem: function (key) {
                delete items[key];
            }
        };
    }
    function fetch(url) {
        var plain = String(url).split("?")[0];
        if (plain === "/api/index") {
            return mapResponse({version: 1, libraries: [{id: "lib", name: "Example"}],
                                albums: [], records: records || MAP_RECORDS});
        }
        if (plain === "/api/locations") {
            return mapResponse(MAP_POINTS);
        }
        if (plain === "/api/people") {
            // (The people come a moment after the pictures' first search.)
            return new Promise(function (resolve) {
                setTimeout(resolve, 40);
            }).then(function () {
                return mapResponse({version: 1, people: MAP_PEOPLE});
            });
        }
        if (plain === "/api/routes") {
            return mapResponse({routes: []});
        }
        return mapResponse(null);
    }
    function Image() {
        this.complete = false;
    }
    var context = {
        document: document,
        navigator: {userAgent: "test", connection: {saveData: false}, clipboard: null,
                    maxTouchPoints: 0},
        location: {search: searchText || "", hash: "", pathname: "/map", protocol: "http:",
                   href: "http://127.0.0.1:5000/map" + (searchText || ""),
                   origin: "http://127.0.0.1:5000"},
        history: {state: null, scrollRestoration: "auto", pushState: function (state, title,
                                                                             url) {
            this.state = state;
            urls.push(String(url));
        }, replaceState: function (state, title, url) {
            this.state = state;
            urls.push(String(url));
        }, back: function () {}},
        localStorage: storage(),
        sessionStorage: storage(),
        innerWidth: 1000,
        innerHeight: 760,
        scrollY: 0,
        devicePixelRatio: 1,
        scrollTo: function () {},
        matchMedia: function () {
            return {matches: false, addEventListener: function () {}, addListener: function () {},
                    removeEventListener: function () {}, removeListener: function () {}};
        },
        requestAnimationFrame: function (callback) {
            return setTimeout(callback, 0);
        },
        cancelAnimationFrame: clearTimeout,
        getComputedStyle: function () {
            return {getPropertyValue: function () {
                return "";
            }};
        },
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
        setInterval: setInterval,
        clearInterval: clearInterval,
        console: {log: function () {}, warn: function () {}, error: function () {}},
        URLSearchParams: URLSearchParams,
        URL: URL,
        Image: Image,
        fetch: fetch
    };
    context.window = context;
    context.addEventListener = function () {};
    context.removeEventListener = function () {};
    vm.createContext(context);
    MAP_SCRIPTS.forEach(function (script) {
        vm.runInContext(script.code, context, {filename: script.name});
    });
    return {
        document: document,
        urls: urls,
        views: function () {
            return urls.map(function (url) {
                var view = new URLSearchParams(url.split("?")[1] || "").get("view");
                return view ? view.split(",").map(Number) : null;
            }).filter(Boolean);
        }
    };
}

// The page's view once the people are in and the address has been written
// after them (a view is written 250 ms after it changes).
function settledView(page) {
    return FAKE.waitFor(function () {
        return /Cleo/.test(page.document.getElementById("map-search").textContent);
    }).then(function () {
        return new Promise(function (resolve) {
            setTimeout(resolve, 400);
        });
    }).then(function () {
        var views = page.views();
        return views[views.length - 1];
    });
}

function nearCleo(view) {
    return Math.abs(view[0] - 45.01) < 0.5 && Math.abs(view[1] - 10.015) < 0.5 && view[2] >= 6;
}

test("a search naming a person fits her pictures once the people are in", function () {
    return Promise.all(["?q=cleo", "?q=person:%22Cleo%22", "?q=cleo+harbour"].map(
        function (address) {
            var page = mapPage(address);
            return settledView(page).then(function (view) {
                assert.ok(view && nearCleo(view), address + " -> " + JSON.stringify(view));
            });
        }));
});

test("a view from the address that shows none of her pictures is fitted to them", function () {
    var away = mapPage("?q=cleo&view=-40,-60,6");
    var shown = mapPage("?q=cleo&view=45,10,4");
    return settledView(away).then(function (view) {
        assert.ok(view && nearCleo(view), JSON.stringify(view));
        return settledView(shown);
    }).then(function (view) {
        // (Her pictures are in sight: the view stays as the address had it.)
        assert.deepStrictEqual(view, [45, 10, 4]);
    });
});

test("a search whose picture has a place but no position says it has no position",
     function () {
    // (A place its file names: place_estimates.file_place, ps "f", no point.)
    var named = {i: 5, l: "lib", t: "Kennel", g: ["dog"], dk: "2024-05-05", ds: 1,
                 ci: "Coralport", co: "Examplia", cc: "XA", ps: "f"};
    var page = mapPage("?q=coralport", MAP_RECORDS.concat([named]));
    var box = page.document.getElementById("map-message");
    return FAKE.waitFor(function () {
        return !box.hidden && /position/.test(box.textContent);
    }).then(function () {
        assert.strictEqual(box.querySelector("h2").textContent,
                           "The picture that matches has no position");
        assert.ok(/a place named without a position has no pin/.test(box.textContent),
                  box.textContent);
        assert.ok(!/no location/.test(box.textContent), box.textContent);
    });
});

test("an export's corner credit of several lines has the other credits above it",
     function () {
    // (export.py render_page puts it in the map's corner: GeoNames, named places and the
    // link to the photo credits, which on a narrow phone take three lines; style.css
    // places the street maps' credit and the note above a corner of one line.)
    var corner = '<p id="map-credit" class="export-credits map-credit">Places: GeoNames' +
        '<br>Named places: OpenStreetMap contributors (ODbL) <a href="index.html#credits">' +
        'Photo credits</a></p>\n';
    var EXPORT_MAP = MAP_PAGE.replace('<div id="map-message"', corner + '<div id="map-message"');
    assert.notStrictEqual(EXPORT_MAP, MAP_PAGE);
    // The map is 700 px tall (mapPage); the corner sits 8 px above its foot.
    function stacked(cornerHeight) {
        return mapPage("", null, EXPORT_MAP, function (document) {
            document.getElementById("map-credit").getBoundingClientRect = function () {
                return {top: 692 - cornerHeight, bottom: 692, left: 640, right: 992,
                        width: 352, height: cornerHeight};
            };
            document.getElementById("map-streets-credit").getBoundingClientRect = function () {
                return {top: 600, bottom: 621, left: 842, right: 992, width: 150, height: 21};
            };
        });
    }
    function settled(page) {
        var streets = page.document.getElementById("map-streets-credit");
        var note = page.document.getElementById("map-note");
        return FAKE.waitFor(function () {
            return streets.style.bottom && note.style.bottom;
        }).then(function () {
            return new Promise(function (resolve) {
                setTimeout(resolve, 100);
            });
        }).then(function () {
            return [streets.hidden, streets.style.bottom, note.style.bottom];
        });
    }
    var plain = mapPage("");
    return Promise.all([settled(stacked(54)), settled(stacked(21))]).then(function (found) {
        // Three lines (54 px): the street maps' credit 5 px above it, the note 11 px
        // above that.
        assert.deepStrictEqual(found[0], [false, "67px", "99px"]);
        // One line: where style.css has them (34 px; 66 px).
        assert.deepStrictEqual(found[1], [false, "34px", "66px"]);
        // The server's map page has no such corner: style.css alone places them.
        assert.ok(!plain.document.getElementById("map-streets-credit").style.bottom);
        assert.ok(!plain.document.getElementById("map-note").style.bottom);
    });
});

/* ---------- Running -------------------------------------------------------------------- */

// (A test may return a promise: the map page's tests wait for the page.)
var failures = 0;
tests.reduce(function (chain, entry) {
    return chain.then(function () {
        return Promise.resolve().then(entry.body).then(function () {
            console.log("ok - " + entry.name);
        }, function (error) {
            failures += 1;
            console.log("FAIL - " + entry.name);
            console.log(String(error && error.stack || error).split("\n").map(function (line) {
                return "    " + line;
            }).join("\n"));
        });
    });
}, Promise.resolve()).then(function () {
    console.log((tests.length - failures) + " of " + tests.length + " passed");
    // (The pages' timers would keep Node waiting.)
    process.exit(failures ? 1 : 0);
});
