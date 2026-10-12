/* Tests for the parts of static/map.js that need no page: the projection
   and the repeating world, grouping pictures into pins (also across the
   date line, and the same wherever the map is panned to), the pin under
   the pointer, the wheel, and fitting the view to the pictures.

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

test("the world is twice as wide as tall and grows twice per zoom", function () {
    assert.strictEqual(MAP.worldWidth(TILE, 0), 512);
    assert.strictEqual(MAP.worldWidth(TILE, 3), 4096);
    var width = MAP.worldWidth(TILE, 2);
    assert.deepStrictEqual(MAP.project(90, -180, width), {x: 0, y: 0});
    assert.deepStrictEqual(MAP.project(0, 0, width), {x: 1024, y: 512});
    assert.deepStrictEqual(MAP.project(-90, 90, width), {x: 1536, y: 1024});
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
    near(view.lat, -17.5);
    assert.ok(Math.abs(Math.abs(view.lon) - 180) < 1e-9, String(view.lon));
    assert.strictEqual(view.zoom, 6);
    // Europe: the view is around it, not around the whole world.
    var europe = MAP.fitView(points([[48.85, 2.35], [52.52, 13.4], [41.9, 12.5], [38.7, -9.1]]),
                             1400, 800, TILE, 0, 6, 40);
    assert.ok(europe.lon > 0 && europe.lon < 5, String(europe.lon));
    near(europe.lat, (38.7 + 52.52) / 2, 1e-9);
    // 22.5 degrees across fit 1,320 px at zoom 5 (1,024 px), not at 6.
    assert.strictEqual(europe.zoom, 5);
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

test("a chip of the viewer opens a new search, without the timeline's dates", function () {
    var state = {q: "beach", lib: "lib_family1", filters: {type: ["photo"]}, from: "2019",
                 to: "2019", view: "20.8,-156.4,9", ids: "", group: true};
    var target = MAP.chipState(state, "place", "United States|Hawaii|Hilo");
    assert.deepStrictEqual(target, {q: "", lib: "lib_family1",
                                    filters: {place: ["United States|Hawaii|Hilo"]},
                                    from: "", to: "", view: "", ids: "", group: true});
    target = MAP.chipState(state, "q", "tag:sunset");
    assert.strictEqual(target.q, "tag:sunset");
    assert.deepStrictEqual(target.filters, {});
    assert.strictEqual(target.from + target.to, "");
    // The state it was made from is left as it was.
    assert.strictEqual(state.from, "2019");
});

/* ---------- Running -------------------------------------------------------------------- */

var failures = 0;
tests.forEach(function (entry) {
    try {
        entry.body();
        console.log("ok - " + entry.name);
    } catch (error) {
        failures += 1;
        console.log("FAIL - " + entry.name);
        console.log(String(error && error.stack || error).split("\n").map(function (line) {
            return "    " + line;
        }).join("\n"));
    }
});
console.log((tests.length - failures) + " of " + tests.length + " passed");
process.exitCode = failures ? 1 : 0;
