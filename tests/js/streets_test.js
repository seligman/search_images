/* Tests for static/streets.js (the maps' ground: the world image in Web
   Mercator, and the street maps of street_maps.py): undoing gzip, reading
   vector tiles, the projection, the world image's tiles redrawn and drawn
   with stand-ins and the sea, which street tiles a view asks for (only
   those the index lists, only from where the page settings say), drawing
   them with their names (kept clear of the map's buttons), overzoom and
   gaps (filled from at most FILL_LEVELS zooms up, never below FROM_ZOOM),
   finer tiles standing in after a zoom out, the time a frame may spend
   drawing tiles, which places get names, and the street maps from
   FROM_ZOOM on only. Every tile here is made up.

   Run by tests/test_street_maps.py, or directly: node tests/js/streets_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");
var zlib = require("zlib");

var STREETS = require(path.join(__dirname, "..", "..", "static", "streets.js"));

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function near(actual, expected, within, message) {
    assert.ok(Math.abs(actual - expected) <= (within || 1e-9),
              (message || "") + " " + actual + " is not about " + expected);
}

/* ---------- A made-up vector tile ------------------------------------------ */

function varint(value, out) {
    while (value >= 128) {
        out.push((value % 128) + 128);
        value = Math.floor(value / 128);
    }
    out.push(value);
}

function key(number, wire, out) {
    varint(number * 8 + wire, out);
}

function bytes(number, list, out) {
    key(number, 2, out);
    varint(list.length, out);
    list.forEach(function (byte) {
        out.push(byte);
    });
}

function text(value) {
    return Array.prototype.slice.call(Buffer.from(value, "utf8"));
}

function zigzag(value) {
    return value < 0 ? -2 * value - 1 : 2 * value;
}

// The commands of a geometry: parts ([[x, y], ...]) as points, a line or rings.
function commands(parts, closed) {
    var out = [];
    var x = 0;
    var y = 0;
    parts.forEach(function (part) {
        part.forEach(function (point, index) {
            if (index === 0) {
                out.push(1 | (1 << 3));
            } else if (index === 1) {
                out.push(2 | ((part.length - 1) << 3));
            }
            out.push(zigzag(point[0] - x), zigzag(point[1] - y));
            x = point[0];
            y = point[1];
        });
        if (closed) {
            out.push(7 | (1 << 3));
        }
    });
    return out;
}

function packed(numbers) {
    var out = [];
    numbers.forEach(function (value) {
        varint(value, out);
    });
    return out;
}

/* A layer: name and features [{type (1 point, 2 line, 3 polygon), props,
   parts}]; string, number and true/false values. */
function layer(name, features) {
    var keys = [];
    var values = [];
    var valueKeys = [];
    var out = [];
    varint(15 * 8, out);
    varint(2, out);
    bytes(1, text(name), out);
    features.forEach(function (feature) {
        var tags = [];
        Object.keys(feature.props).forEach(function (prop) {
            var value = feature.props[prop];
            if (keys.indexOf(prop) < 0) {
                keys.push(prop);
            }
            var id = typeof value + ":" + value;
            if (valueKeys.indexOf(id) < 0) {
                valueKeys.push(id);
                values.push(value);
            }
            tags.push(keys.indexOf(prop), valueKeys.indexOf(id));
        });
        var body = [];
        key(3, 0, body);
        varint(feature.type, body);
        bytes(2, packed(tags), body);
        bytes(4, packed(commands(feature.parts, feature.type === 3)), body);
        bytes(2, body, out);
    });
    keys.forEach(function (prop) {
        bytes(3, text(prop), out);
    });
    values.forEach(function (value) {
        var body = [];
        if (typeof value === "string") {
            bytes(1, text(value), body);
        } else if (typeof value === "boolean") {
            key(7, 0, body);
            varint(value ? 1 : 0, body);
        } else {
            key(5, 0, body);
            varint(value, body);
        }
        bytes(4, body, out);
    });
    key(5, 0, out);
    varint(4096, out);
    return out;
}

function tileBytes(layers) {
    var out = [];
    layers.forEach(function (one) {
        bytes(3, one, out);
    });
    return Buffer.from(out);
}

var SQUARE = [[[0, 0], [4096, 0], [4096, 4096], [0, 4096]]];

function sampleTile() {
    return tileBytes([
        layer("earth", [{type: 3, props: {kind: "earth"}, parts: SQUARE}]),
        layer("water", [{type: 3, props: {kind: "water"},
                         parts: [[[100, 100], [600, 100], [600, 600], [100, 600]]]},
                        {type: 1, props: {kind: "water", name: "Test Lake"},
                         parts: [[[350, 350]]]}]),
        layer("roads", [{type: 2, props: {kind: "major_road", name: "Long Test Road",
                                          sort_rank: 400},
                         parts: [[[0, 2048], [4096, 2048]]]},
                        {type: 2, props: {kind: "highway", is_tunnel: true},
                         parts: [[[2048, 0], [2048, 4096]]]}]),
        layer("places", [{type: 1, props: {kind: "locality", name: "Testtown",
                                           population_rank: 10},
                          parts: [[[1000, 1000]]]}]),
        layer("buildings", [{type: 3, props: {kind: "building"},
                             parts: [[[3000, 3000], [3200, 3000], [3200, 3200],
                                      [3000, 3200]]]},
                            {type: 1, props: {kind: "address", addr_housenumber: "7"},
                             parts: [[[3100, 3100]]]}]),
        layer("pois", [{type: 1, props: {kind: "bench"}, parts: [[[5, 5]]]}])
    ]);
}

/* ---------- Canvases that write down what is drawn on them ------------------ */

function recorder() {
    var calls = [];
    var ctx = {calls: calls, globalAlpha: 1};
    ["setTransform", "fillRect", "beginPath", "moveTo", "lineTo", "closePath", "fill",
     "stroke", "setLineDash", "drawImage", "save", "restore", "translate", "rotate",
     "strokeText", "fillText"].forEach(function (name) {
        ctx[name] = function () {
            calls.push([name].concat(Array.prototype.slice.call(arguments)));
        };
    });
    ctx.measureText = function (value) {
        return {width: value.length * 6};
    };
    ctx.createLinearGradient = function () {
        calls.push(["createLinearGradient"].concat(Array.prototype.slice.call(arguments)));
        return {addColorStop: function () {}};
    };
    return ctx;
}

var canvases = [];

function makeCanvas(w, h) {
    var ctx = recorder();
    var canvas = {width: w, height: h, ctx: ctx, getContext: function () {
        return ctx;
    }};
    canvases.push(canvas);
    return canvas;
}

function named(ctx, name) {
    return ctx.calls.filter(function (call) {
        return call[0] === name;
    });
}

/* ---------- gzip and vector tiles ------------------------------------------------ */

test("gzip is undone as zlib does it, stored, fixed and dynamic blocks alike", function () {
    var parts = [];
    for (var k = 0; k < 4000; k++) {
        parts.push("street " + (k % 97) + " " + String.fromCharCode(65 + (k * 7) % 26));
    }
    var plain = Buffer.from(parts.join(";"), "utf8");
    [0, 1, 6, 9].forEach(function (level) {
        var packedBytes = zlib.gzipSync(plain, {level: level});
        var back = STREETS.inflate(new Uint8Array(packedBytes));
        assert.strictEqual(Buffer.compare(Buffer.from(back), plain), 0, "level " + level);
    });
    // Short data (a fixed block) and raw deflate data.
    var small = Buffer.from("abcabcabcabc");
    assert.strictEqual(Buffer.from(STREETS.inflate(new Uint8Array(zlib.gzipSync(small))))
        .toString(), "abcabcabcabc");
    assert.strictEqual(Buffer.from(STREETS.inflate(new Uint8Array(zlib.deflateRawSync(small))))
        .toString(), "abcabcabcabc");
    assert.throws(function () {
        STREETS.inflate(new Uint8Array([0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff, 0xff,
                                        0xff, 0xff, 0xff, 0xff, 0xff, 0xff]));
    });
});

test("a vector tile reads back: layers, properties and geometry; points of no use left out",
     function () {
         var layers = STREETS.decodeTile(new Uint8Array(sampleTile()));
         assert.deepStrictEqual(Object.keys(layers).sort(),
                                ["buildings", "earth", "places", "roads", "water"]);
         var road = layers.roads.features[0];
         assert.strictEqual(road.type, 2);
         assert.deepStrictEqual(road.props, {kind: "major_road", name: "Long Test Road",
                                             sort_rank: 400});
         assert.deepStrictEqual(Array.prototype.slice.call(road.geom[0]), [0, 2048, 4096, 2048]);
         assert.strictEqual(layers.roads.features[1].props.is_tunnel, true);
         assert.strictEqual(layers.roads.extent, 4096);
         assert.deepStrictEqual(Array.prototype.slice.call(layers.water.features[0].geom[0]),
                                [100, 100, 600, 100, 600, 600, 100, 600]);
         // (The address point of the buildings layer is left out.)
         assert.strictEqual(layers.buildings.features.length, 1);
         assert.strictEqual(layers.places.features[0].props.population_rank, 10);
         // Gzip and base64, as the tile scripts carry them.
         var b64 = zlib.gzipSync(sampleTile()).toString("base64");
         var again = STREETS.decodeTile(STREETS.inflate(STREETS.base64Bytes(b64)));
         assert.strictEqual(again.places.features[0].props.name, "Testtown");
     });

test("Web Mercator: 0 at the north edge, 1 at the south, the poles left out", function () {
    near(STREETS.mercatorY(0), 0.5);
    near(STREETS.mercatorY(STREETS.MAX_LAT), 0, 1e-9);
    near(STREETS.mercatorY(-STREETS.MAX_LAT), 1, 1e-9);
    assert.strictEqual(STREETS.mercatorY(90), STREETS.mercatorY(STREETS.MAX_LAT));
    [-60, -12.5, 0, 33.3, 55.68, 80].forEach(function (lat) {
        near(STREETS.latitudeAt(STREETS.mercatorY(lat)), lat, 1e-9);
    });
});

test("line widths grow along their curve between the zooms given", function () {
    var table = [[10, 1], [12, 4]];
    assert.strictEqual(STREETS.width(table, 9), 0);
    assert.strictEqual(STREETS.width(table, 10), 1);
    near(STREETS.width(table, 11), 2);
    assert.strictEqual(STREETS.width(table, 12), 4);
    assert.strictEqual(STREETS.width(table, 20), 4);
    // Every road kind gets wider zoom by zoom.
    Object.keys(STREETS.WIDTHS).forEach(function (kind) {
        for (var zoom = 1; zoom <= 18; zoom++) {
            assert.ok(STREETS.width(STREETS.WIDTHS[kind], zoom) >=
                      STREETS.width(STREETS.WIDTHS[kind], zoom - 1), kind + " " + zoom);
        }
    });
});

test("a street tile is drawn with its roads, waters, buildings and names", function () {
    canvases = [];
    var data = STREETS.decodeTile(new Uint8Array(sampleTile()));
    var drawn = STREETS.renderTile(makeCanvas, data, 15, 100, 200, 15, 2, "light");
    assert.strictEqual(drawn.canvas.width, 1024);
    var ctx = drawn.canvas.ctx;
    // The water first, then the land over it.
    var fills = named(ctx, "fillRect");
    assert.deepStrictEqual(fills[0].slice(1), [0, 0, 512, 512]);
    assert.ok(named(ctx, "fill").length >= 3);
    // The road at the middle row of the tile, in tile pixels (4096 -> 512).
    var moves = named(ctx, "moveTo").map(function (call) {
        return call.slice(1).join(",");
    });
    assert.ok(moves.indexOf("0,256") >= 0, moves.join(" "));
    var names = drawn.labels.map(function (label) {
        return label.text;
    }).sort();
    assert.deepStrictEqual(names, ["Long Test Road", "Test Lake", "Testtown"]);
    var town = drawn.labels.filter(function (label) {
        return label.text === "Testtown";
    })[0];
    assert.deepStrictEqual([town.x, town.y], [125, 125]);
    // A place outranks a road's name.
    var road = drawn.labels.filter(function (label) {
        return label.text === "Long Test Road";
    })[0];
    assert.ok(town.prio < road.prio);
    // Dark: other colours.
    var dark = STREETS.renderTile(makeCanvas, data, 15, 100, 200, 15, 1, "dark");
    assert.notStrictEqual(dark.canvas.ctx.fillStyle, ctx.fillStyle);
    // Two zooms past the tile's own: its quarter... of a quarter, twice as large.
    var over = STREETS.renderTile(makeCanvas, data, 17, 4 * 100 + 1, 4 * 200 + 2, 15, 1,
                                  "light");
    var overMoves = named(over.canvas.ctx, "moveTo").map(function (call) {
        return call.slice(1).join(",");
    });
    // (The road at y 2048 of 4096 is at 4 * 256 - 2 * 512 = 0 of the tile two rows down.)
    assert.ok(overMoves.indexOf("-512,0") >= 0, overMoves.join(" "));
});

/* ---------- The world image ------------------------------------------------------- */

test("the world image is redrawn into Web Mercator and drawn with stand-ins and the sea",
     function () {
         var made = [];
         global.Image = function () {
             made.push(this);
         };
         try {
             canvases = [];
             var asked = [];
             var loads = 0;
             var layer = new STREETS.WorldLayer({tileUrl: function (z, x, y) {
                 asked.push(z + "/" + x + "/" + y);
                 return "tile/" + z + "/" + x + "/" + y + ".jpg";
             }, tileSize: 256, tileZoom: 1, ocean: "#123456", makeCanvas: makeCanvas,
                                                onLoad: function () {
                                                    loads += 1;
                                                }});
             // Zoom 0: the world is 512 px wide and tall; the view of 600 px from x
             // -50 sees parts of four columns of zoom 0 (its two tiles, twice).
             var ctx = recorder();
             assert.strictEqual(layer.draw(ctx, 0, -50, 0, 600, 512), 0);
             assert.deepStrictEqual(asked, ["0/1/0", "0/0/0"]);
             assert.strictEqual(named(ctx, "fillRect").length, 4);
             assert.strictEqual(ctx.fillStyle, "#123456");
             // A tile that arrives is redrawn once, row by row, into a canvas as
             // tall as its rows are in Web Mercator (the whole world: 512), and
             // drawn from then on (and asks for a new drawing).
             made[1].onload();
             assert.strictEqual(loads, 1);
             var merc = canvases[canvases.length - 1];
             assert.deepStrictEqual([merc.width, merc.height], [256, 512]);
             var rows = named(merc.ctx, "drawImage");
             // (No row nearer the poles than 85 degrees.)
             assert.ok(rows.length > 200 && rows.length < 256, String(rows.length));
             assert.ok(rows.every(function (call) {
                 return call[1] === made[1] && call[5] === 1 && call[9] >= 1;
             }));
             ctx = recorder();
             layer.draw(ctx, 0, 0, 0, 512, 512);
             var images = named(ctx, "drawImage");
             assert.strictEqual(images.length, 1);
             assert.strictEqual(images[0][1], merc);
             assert.deepStrictEqual(images[0].slice(2), [0, 0, 256, 512]);
             assert.strictEqual(named(ctx, "fillRect").length, 1);
             // Past the tiles' detail: stretched, fainter, and a loaded coarser tile
             // stands in for one still loading (a part of it, nine arguments).
             ctx = recorder();
             assert.strictEqual(layer.draw(ctx, 3, 0, 0, 256, 256), 2);
             var stand = named(ctx, "drawImage");
             assert.ok(stand.length >= 1 && stand[0].length === 10, JSON.stringify(stand[0]));
             assert.strictEqual(ctx.globalAlpha, 1);
             // The rows of a tile further south start lower: row 1 of zoom 1 (the
             // southern half) is drawn from y 512 of the 1024 px world.
             layer.draw(recorder(), 1, 0, 0, 1024, 1024);
             made.slice(2).forEach(function (image) {
                 image.onload();
             });
             ctx = recorder();
             layer.draw(ctx, 1, 0, 0, 1024, 1024);
             var south = named(ctx, "drawImage").filter(function (call) {
                 return call[3] === 512;
             });
             assert.strictEqual(south.length, 4);
             assert.strictEqual(south[0][5], 512);
         } finally {
             delete global.Image;
         }
     });

/* ---------- The street maps ------------------------------------------------------- */

test("street tiles: only those the index lists, from where the page says, drawn with names",
     function () {
         canvases = [];
         var info = {url: "maps-a/", v: "v1"};
         var source = STREETS.sourceFor(info);
         var asked = [];
         source.load = function (src) {
             asked.push(src);
         };
         var loads = 0;
         var layer = new STREETS.StreetLayer({streets: info, makeCanvas: makeCanvas,
                                              onLoad: function () {
                                                  loads += 1;
                                              }});
         assert.deepStrictEqual(asked, ["maps-a/index.js?v=v1"]);
         // Before the index: nothing drawn, the place counted as without streets.
         var ctx = recorder();
         assert.deepStrictEqual(layer.draw(ctx, 14, 0, 0, 512, 512),
                                {drawn: 0, missing: 1, tiles: 1});
         // The index: zoom 14 tiles 5..6 of row 3, and their parents.
         STREETS.DATA.index({format: 1, minzoom: 0, maxzoom: 14,
                             rows: {"14": [[3, 5, 6]], "13": [[1, 2, 3]], "12": [[0, 1, 1]]}});
         assert.ok(loads >= 1);
         assert.ok(source.has(14, 5, 3) && source.has(14, 6, 3));
         assert.ok(!source.has(14, 4, 3) && !source.has(14, 7, 3) && !source.has(14, 5, 2));
         // A view of tiles 4..7 of row 3 at zoom 14: only 5 and 6 are there; 4 and 7
         // are filled from their parents of zoom 13.
         asked = [];
         ctx = recorder();
         var result = layer.draw(ctx, 14, 4 * 512, 3 * 512, 4 * 512, 512);
         assert.deepStrictEqual(asked.sort(), ["maps-a/13/2/1.js?v=v1", "maps-a/13/3/1.js?v=v1",
                                               "maps-a/14/5/3.js?v=v1",
                                               "maps-a/14/6/3.js?v=v1"]);
         assert.strictEqual(result.drawn, 0);
         assert.strictEqual(result.missing, 0);
         // A tile arrives (its script calls STREETS_DATA.tile): drawn, with its names.
         var b64 = zlib.gzipSync(sampleTile()).toString("base64");
         loads = 0;
         STREETS.DATA.tile(14, 5, 3, b64);
         assert.strictEqual(loads, 1);
         ctx = recorder();
         result = layer.draw(ctx, 14, 4 * 512, 3 * 512, 4 * 512, 512);
         assert.strictEqual(result.drawn, 1);
         var texts = named(ctx, "fillText").map(function (call) {
             return call[1];
         });
         assert.ok(texts.indexOf("Testtown") >= 0, texts.join(","));
         assert.ok(texts.indexOf("Long Test Road") >= 0, texts.join(","));
         // Every address asked for is the street maps' own.
         asked.forEach(function (src) {
             assert.ok(src.indexOf("maps-a/") === 0, src);
         });
         // Far from the index's tiles there is nothing to draw: missing.
         ctx = recorder();
         result = layer.draw(ctx, 14, 900 * 512, 900 * 512, 512, 512);
         assert.deepStrictEqual(result, {drawn: 0, missing: 1, tiles: 1});
         // Zoomed in past the tiles' detail: tile 14/5/3 drawn larger (no new
         // script), up to OVERZOOM zooms.
         asked = [];
         ctx = recorder();
         result = layer.draw(ctx, 16, 4 * 5 * 512, 4 * 3 * 512, 512, 512);
         assert.strictEqual(result.drawn, 1);
         assert.deepStrictEqual(asked, []);
         // A tile that fails to load (asked for above) is not asked for again at
         // once, only after a while.
         asked = [];
         source.failed("14/6/3");
         layer.draw(recorder(), 14, 6 * 512, 3 * 512, 512, 512);
         assert.deepStrictEqual(asked, []);
         source.tiles["14/6/3"].at -= 60000;
         layer.draw(recorder(), 14, 6 * 512, 3 * 512, 512, 512);
         assert.deepStrictEqual(asked, ["maps-a/14/6/3.js?v=v1"]);
     });

test("the street maps show from FROM_ZOOM on (where the world image has no more detail)",
     function () {
         canvases = [];
         var info = {url: "maps-b/", v: ""};
         var source = STREETS.sourceFor(info);
         source.load = function () {};
         global.Image = function () {};
         try {
             var map = new STREETS.Basemap({tileUrl: function () {
                 return "t.jpg";
             }, tileSize: 256, tileZoom: 6, streets: info, makeCanvas: makeCanvas});
             // (Before the index: not known.)
             assert.strictEqual(map.ready(), false);
             assert.strictEqual(map.coveredZoom(10, 10), null);
             assert.strictEqual(map.fitZoom(), STREETS.FIT_ZOOM);
             STREETS.DATA.index({minzoom: 0, maxzoom: 14,
                                 rows: {"7": [[3, 5, 5]], "8": [[6, 10, 10]],
                                        "9": [[12, 20, 20]]}});
             assert.ok(map.ready());
             assert.strictEqual(STREETS.FROM_ZOOM, 9);
             var b64 = zlib.gzipSync(sampleTile()).toString("base64");
             map.draw(recorder(), 9, 20 * 512, 12 * 512, 512, 512);
             STREETS.DATA.tile(9, 20, 12, b64);
             // At zoom 6 (the world image's own), and 7 and 8 (the world image a
             // little stretched: no patchwork of street tiles around the pictures),
             // no street map, though there are tiles of zoom 7 and 8.
             assert.strictEqual(map.draw(recorder(), 6, 0, 0, 512, 512).streets, 0);
             var seven = map.draw(recorder(), 7, 5 * 512, 3 * 512, 512, 512);
             assert.deepStrictEqual([seven.streets, seven.tiles], [0, 0]);
             var eight = map.draw(recorder(), 8, 10 * 512, 6 * 512, 512, 512);
             assert.deepStrictEqual([eight.streets, eight.tiles], [0, 0]);
             // At zoom 9: the street tile there is.
             var found = map.draw(recorder(), 9, 20 * 512, 12 * 512, 512, 512);
             assert.strictEqual(found.streets, 1);
             assert.strictEqual(found.missing, 0);
             assert.strictEqual(found.tiles, 1);
             assert.strictEqual(found.stretch, 3);
             // As deep as the index goes (and fits no deeper than it).
             assert.strictEqual(map.maxZoom(), 14 + STREETS.OVERZOOM);
             assert.strictEqual(map.fitZoom(), 14);
             // The most detailed street tile at a place: tile 9/20/12's middle.
             var lon = 20.5 / 512 * 360 - 180;
             var lat = STREETS.latitudeAt(12.5 / 512);
             assert.strictEqual(map.coveredZoom(lat, lon), 9);
             assert.strictEqual(map.coveredZoom(-40, 100), -1);
             // Without street maps: none, and no deeper zoom.
             var plain = new STREETS.Basemap({tileUrl: function () {
                 return "t.jpg";
             }, tileSize: 256, tileZoom: 6, makeCanvas: makeCanvas});
             assert.strictEqual(plain.draw(recorder(), 9, 0, 0, 512, 512).streets, 0);
             assert.strictEqual(plain.maxZoom(), null);
             assert.strictEqual(plain.fitZoom(), null);
             assert.strictEqual(plain.coveredZoom(lat, lon), null);
         } finally {
             delete global.Image;
         }
     });

// A street layer of its own made-up index (url: a source of its own).
function streetLayer(url, rows, maxzoom) {
    var info = {url: url, v: ""};
    var source = STREETS.sourceFor(info);
    var asked = [];
    source.load = function (src) {
        asked.push(src);
    };
    var layer = new STREETS.StreetLayer({streets: info, makeCanvas: makeCanvas});
    STREETS.DATA.index({minzoom: 0, maxzoom: maxzoom || 15, rows: rows});
    return {layer: layer, source: source, asked: asked};
}

test("a gap is filled from at most FILL_LEVELS zooms coarser, and never below FROM_ZOOM",
     function () {
         canvases = [];
         // A tile of zoom 9 (9/272/192), and two of zoom 8: its parent (8/136/96)
         // and one with no tile of zoom 9 under it (8/137/96).
         var made = streetLayer("maps-c/", {"8": [[96, 136, 137]], "9": [[192, 272, 272]]});
         var layer = made.layer;
         assert.strictEqual(STREETS.FILL_LEVELS, 3);
         // Zoom 12 under the zoom 9 tile: filled from it (three zooms up).
         var x12 = 272 * 8;
         var y12 = 192 * 8;
         assert.deepStrictEqual(layer.draw(recorder(), 12, x12 * 512, y12 * 512, 512, 512),
                                {drawn: 0, missing: 0, tiles: 1});
         assert.ok(made.asked.indexOf("maps-c/9/272/192.js") >= 0, made.asked.join(" "));
         // Zoom 13: four zooms below it, too coarse: missing (the world image shows).
         assert.deepStrictEqual(layer.draw(recorder(), 13, 2 * x12 * 512, 2 * y12 * 512,
                                           512, 512), {drawn: 0, missing: 1, tiles: 1});
         // Zoom 10 under the zoom 8 tile without a zoom 9 one: missing (a tile of zoom 8
         // is never drawn: in a trip's export, home has none of zoom 9 and up).
         made.asked.length = 0;
         assert.deepStrictEqual(layer.draw(recorder(), 10, 137 * 4 * 512, 96 * 4 * 512,
                                           512, 512), {drawn: 0, missing: 1, tiles: 1});
         assert.deepStrictEqual(made.asked, []);
     });

test("zooming out, the finer tiles just drawn stand in for those still on their way",
     function () {
         canvases = [];
         var made = streetLayer("maps-d/", {"13": [[1, 2, 3]], "14": [[3, 4, 7]]});
         var layer = made.layer;
         var b64 = zlib.gzipSync(sampleTile()).toString("base64");
         layer.draw(recorder(), 14, 4 * 512, 3 * 512, 4 * 512, 512);
         [4, 5, 6, 7].forEach(function (x) {
             STREETS.DATA.tile(14, x, 3, b64);
         });
         var drawn = 0;
         for (var tries = 0; tries < 8 && drawn < 4; tries++) {
             drawn = layer.draw(recorder(), 14, 4 * 512, 3 * 512, 4 * 512, 512).drawn;
         }
         assert.strictEqual(drawn, 4);
         // Zoom 13: its two tiles are on their way; the four of zoom 14 (the lower
         // halves of them) stand in, half as large.
         var ctx = recorder();
         var result = layer.draw(ctx, 13, 2 * 512, 1 * 512, 2 * 512, 512);
         assert.strictEqual(result.drawn, 2);
         var images = named(ctx, "drawImage").map(function (call) {
             return call.slice(2).join(",");
         }).sort();
         assert.deepStrictEqual(images, ["0,256,256,256", "256,256,256,256",
                                         "512,256,256,256", "768,256,256,256"]);
     });

test("a tile on its way shows the land's colour; one whose script failed, the world image",
     function () {
         var made = streetLayer("maps-g/", {"14": [[3, 4, 4]]});
         var ctx = recorder();
         assert.deepStrictEqual(made.layer.draw(ctx, 14, 4 * 512, 3 * 512, 512, 512),
                                {drawn: 0, missing: 0, tiles: 1});
         assert.strictEqual(named(ctx, "fillRect").length, 1);
         assert.strictEqual(ctx.fillStyle, STREETS.THEMES.light.earth);
         made.source.failed("14/4/3");
         ctx = recorder();
         assert.deepStrictEqual(made.layer.draw(ctx, 14, 4 * 512, 3 * 512, 512, 512),
                                {drawn: 0, missing: 1, tiles: 1});
         assert.strictEqual(named(ctx, "fillRect").length, 0);
     });

test("tiles are drawn on their canvases for at most FRAME_BUDGET ms a frame (at least one)",
     function () {
         canvases = [];
         var made = streetLayer("maps-e/", {"14": [[3, 4, 7]]});
         var layer = made.layer;
         var b64 = zlib.gzipSync(sampleTile()).toString("base64");
         layer.draw(recorder(), 14, 4 * 512, 3 * 512, 4 * 512, 512);
         [4, 5, 6, 7].forEach(function (x) {
             STREETS.DATA.tile(14, x, 3, b64);
         });
         var realNow = Date.now;
         var clock = 1000;
         Date.now = function () {
             clock += 5;             // (each tile takes 5 ms to draw)
             return clock;
         };
         try {
             assert.strictEqual(layer.draw(recorder(), 14, 4 * 512, 3 * 512, 4 * 512,
                                           512).drawn, 2);
             clock += 1000;
             assert.strictEqual(layer.draw(recorder(), 14, 4 * 512, 3 * 512, 4 * 512,
                                           512).drawn, 4);
         } finally {
             Date.now = realNow;
         }
     });

test("where the street maps end, a tile fades out on that side into the world image",
     function () {
         canvases = [];
         var made = streetLayer("maps-h/", {"13": [[1, 2, 2]], "14": [[3, 4, 5]]});
         // 14/4/3: to the east 14/5/3, to the north a part of 13/2/1 (a fill); none
         // to the west or south.
         assert.strictEqual(made.layer.edges(14, 4, 3), 1 | 8);
         assert.strictEqual(made.layer.edges(14, 5, 3), 4 | 8);
         var data = STREETS.decodeTile(new Uint8Array(sampleTile()));
         var faded = STREETS.renderTile(makeCanvas, data, 14, 4, 3, 14, 1, "light", 1 | 8);
         var gradients = named(faded.canvas.ctx, "createLinearGradient").map(function (call) {
             return call.slice(1).join(",");
         });
         assert.deepStrictEqual(gradients, ["0,0,48,0", "0,512,0,464"]);
         var plain = STREETS.renderTile(makeCanvas, data, 14, 4, 3, 14, 1, "light", 0);
         assert.strictEqual(named(plain.canvas.ctx, "createLinearGradient").length, 0);
     });

test("place names: from their min_zoom on; a place that is no town is a minor name",
     function () {
         var town = {kind: "locality", kind_detail: "town", population_rank: 6, min_zoom: 10};
         assert.strictEqual(STREETS.placeLabel(town, 9), null);
         var shown = STREETS.placeLabel(town, 12);
         assert.ok(/ 13px /.test(shown.font), shown.font);
         assert.strictEqual(shown.colour, "text");
         var city = STREETS.placeLabel({kind: "locality", kind_detail: "city",
                                        population_rank: 12, min_zoom: 4}, 12);
         assert.ok(/^600 16px /.test(city.font), city.font);
         assert.ok(city.prio < shown.prio);
         // A crossroads or a bastion (kind_detail "locality"): not before its
         // min_zoom, then small and grey, after the neighbourhoods.
         var bastion = {kind: "locality", kind_detail: "locality", population_rank: 3,
                        min_zoom: 13};
         assert.strictEqual(STREETS.placeLabel(bastion, 12), null);
         var minor = STREETS.placeLabel(bastion, 13);
         assert.ok(/^11px /.test(minor.font), minor.font);
         assert.strictEqual(minor.colour, "minor");
         var suburb = STREETS.placeLabel({kind: "neighbourhood", kind_detail: "suburb",
                                          min_zoom: 13}, 13);
         assert.ok(minor.prio > suburb.prio && minor.prio > shown.prio);
         // (A place without kind_detail or min_zoom: by its rank, as before.)
         assert.ok(STREETS.placeLabel({kind: "locality", population_rank: 10}, 9));
     });

test("names keep clear of the map's buttons and credit (draw's avoid)", function () {
    canvases = [];
    var made = streetLayer("maps-f/", {"15": [[200, 100, 100]]});
    var layer = made.layer;
    var b64 = zlib.gzipSync(sampleTile()).toString("base64");
    layer.draw(recorder(), 15, 100 * 512, 200 * 512, 512, 512);
    STREETS.DATA.tile(15, 100, 200, b64);
    function texts(avoid) {
        var ctx = recorder();
        layer.draw(ctx, 15, 100 * 512, 200 * 512, 512, 512, avoid);
        return named(ctx, "fillText").map(function (call) {
            return call[1];
        });
    }
    // "Testtown" is at (125, 125) of the tile.
    assert.ok(texts([]).indexOf("Testtown") >= 0);
    assert.ok(texts([[100, 100, 150, 150]]).indexOf("Testtown") < 0);
    assert.ok(texts([[300, 300, 400, 400]]).indexOf("Testtown") >= 0);
});

test("the credit's licence link is a file of the street maps' own place", function () {
    assert.strictEqual(STREETS.licenceUrl({url: "/maps/", v: "x"}), "/maps/LICENSE.txt");
    assert.strictEqual(STREETS.licenceUrl({url: "maps/", v: ""}), "maps/LICENSE.txt");
    assert.strictEqual(STREETS.licenceUrl(null), null);
});

test("the credit names OpenStreetMap, in ASCII-safe form", function () {
    assert.strictEqual(STREETS.ATTRIBUTION, "\u00a9 OpenStreetMap contributors");
});

/* ---------- Running -------------------------------------------------------------- */

var failures = 0;
tests.forEach(function (entry) {
    try {
        entry.body();
        console.log("ok   " + entry.name);
    } catch (error) {
        failures += 1;
        console.log("FAIL " + entry.name);
        console.log(String(error && error.stack || error).split("\n").map(function (line) {
            return "     " + line;
        }).join("\n"));
    }
});
console.log((tests.length - failures) + " of " + tests.length + " passed");
process.exit(failures ? 1 : 0);
