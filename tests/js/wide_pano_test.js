/* Tests for wide pictures (flat panoramas) in the viewer: the pure helpers of
   static/viewer.js (VIEWER.format: when a picture opens in its pan view,
   where, which drags change picture), SITE.panoUrl for their larger picture
   (static/site.js), and VIEWER.Stage on a made-up page: the pan view, the
   stage resized (a phone turned, the information pane opened), a drag and a
   swipe at the end, a double tap, the minimap and its keys, the larger
   picture swapped in.

   Run by tests/test_wide_panoramas.py, or directly:
       node tests/js/wide_pano_test.js
   Prints one line per test and exits with status 1 when any test fails.
*/
"use strict";

var assert = require("assert");
var path = require("path");

var STATIC = path.join(__dirname, "..", "..", "static");
var VIEWER = require(path.join(STATIC, "viewer.js"));
var format = VIEWER.format;

var tests = [];

function test(name, body) {
    tests.push({name: name, body: body});
}

function near(actual, expected, message, tolerance) {
    assert.ok(Math.abs(actual - expected) <= (tolerance || 1e-6),
              (message || "") + ": " + actual + " is not " + expected);
}

// site.js reads its settings once, when it loads: load it afresh for a page.
function loadSite(config) {
    var file = require.resolve(path.join(STATIC, "site.js"));
    delete require.cache[file];
    global.window = {APP_CONFIG: config, location: {search: "", protocol: "http:"}};
    try {
        return require(file);
    } finally {
        delete global.window;
        delete require.cache[file];
    }
}

/* ---------- Pure helpers ------------------------------------------------------ */

test("a wide picture is a flat photo at least 2.2 times as wide as it is tall", function () {
    assert.strictEqual(format.WIDE_ASPECT, 2.2);
    assert.ok(format.isWide({i: 1, w: 2200, hgt: 1000}));
    assert.ok(format.isWide({i: 1, w: 16150, hgt: 3782}));
    assert.ok(!format.isWide({i: 1, w: 2199, hgt: 1000}));
    assert.ok(!format.isWide({i: 1, w: 4000, hgt: 2000}), "2:1");
    assert.ok(!format.isWide({i: 1, w: 3000, hgt: 4000}), "upright");
    assert.ok(!format.isWide({i: 1, w: 6000, hgt: 1000, p: "360"}), "a 360 photo");
    assert.ok(!format.isWide({i: 1, w: 1920, hgt: 800, mv: 1}), "a video");
    assert.ok(!format.isWide({i: 1}));
    assert.ok(!format.isWide(null));
});

test("the pan view fills an upright phone's stage, not a desktop's or a phone held sideways", function () {
    // Fitted whole across a 390 x 792 stage, a 4.27:1 panorama is 91 px tall.
    var fill = format.panFill(390, 390 / 4.27, 390, 792);
    near(fill, 792 / (390 / 4.27), "fills the height");
    // With the bottom sheet at its peek height too (390 x 454).
    assert.ok(format.panFill(390, 390 / 2.2, 390, 454) > 2.5);
    // Below WIDE_ASPECT: shown whole.
    assert.strictEqual(format.panFill(390, 390 / 2.1, 390, 792), 0);
    // (A preview a hair narrower than its wide original's 2.2:1 still pans.)
    assert.ok(format.panFill(390, 390 * 931 / 2048, 390, 792) > 4);
    // A desktop window: even a 4.27:1 panorama is 239 px tall whole.
    assert.strictEqual(format.panFill(1020, 1020 / 4.27, 1020, 850), 0);
    // ... a much wider one is a strip there too.
    assert.ok(format.panFill(1020, 1020 / 6, 1020, 850) > 1);
    // A phone held sideways (844 x 338): not much narrower than the picture.
    assert.strictEqual(format.panFill(844, 844 / 4.27, 844, 338), 0);
    assert.strictEqual(format.panFill(0, 0, 390, 792), 0);
    assert.strictEqual(format.panFill(390, 90, 0, 0), 0);
});

test("the pan view keeps a point of the picture in the middle, without gaps", function () {
    // A picture 3000 px wide on a stage 400 px wide.
    assert.strictEqual(format.panOffset(0.5, 3000, 400), 200 - 1500);
    assert.strictEqual(format.panOffset(0, 3000, 400), 0, "no gap at the left");
    assert.strictEqual(format.panOffset(1, 3000, 400), 400 - 3000, "nor at the right");
    near(format.panCentre(format.panOffset(0.3, 3000, 400), 3000, 400), 0.3, "round trip");
    // No wider than the stage: centred.
    assert.strictEqual(format.panOffset(0.1, 300, 400), 50);
    assert.strictEqual(format.panCentre(50, 300, 400), 0.5);
    // How far along: 0 at the left end, 1 at the right end.
    assert.strictEqual(format.panAlong(0, 3000, 400), 0);
    assert.strictEqual(format.panAlong(-2600, 3000, 400), 1);
    near(format.panAlong(-1300, 3000, 400), 0.5, "half way");
});

test("the minimap frames the part of the picture in view", function () {
    var view = format.panWindow(-1300, 0, 3000, 800, 400, 800);
    near(view.left, 1300 / 3000, "left");
    near(view.width, 400 / 3000, "width");
    assert.strictEqual(view.top, 0);
    assert.strictEqual(view.height, 1);
    // Zoomed in further: part of its height too.
    view = format.panWindow(-100, -400, 6000, 1600, 400, 800);
    near(view.top, 0.25, "top");
    near(view.height, 0.5, "height");
});

test("a swipe changes picture only when it begins at an end and pushes on beyond it", function () {
    // Which end the view is at (within slack px).
    assert.strictEqual(format.panEnd(0, 3000, 400), -1);
    assert.strictEqual(format.panEnd(-2600, 3000, 400), 1);
    assert.strictEqual(format.panEnd(-1300, 3000, 400), 0);
    assert.strictEqual(format.panEnd(-20, 3000, 400), 0);
    assert.strictEqual(format.panEnd(-20, 3000, 400, format.PAN_END_SLACK), -1);
    assert.strictEqual(format.panEnd(0, 400, 400), 0, "no wider than the stage");
    // At the right end a swipe to the left goes on to the next picture...
    assert.strictEqual(format.panSwipe(1, -80, 5), 1);
    // ... at the left end a swipe to the right to the previous one.
    assert.strictEqual(format.panSwipe(-1, 80, 5), -1);
    // Short, the other way, mostly up or down, or not from an end: it only pans.
    assert.strictEqual(format.panSwipe(1, -50, 0), 0);
    assert.strictEqual(format.panSwipe(1, 80, 0), 0);
    assert.strictEqual(format.panSwipe(-1, 80, 70), 0);
    assert.strictEqual(format.panSwipe(0, -200, 0), 0);
});

test("a wide photo's larger picture comes from /pano, or an export's panos folder", function () {
    var SITE = loadSite({mode: "server", api: "/api", page: "search"});
    // As images.wide_pano_size: a picture taller than its preview by enough.
    assert.strictEqual(SITE.panoUrl({i: 7, w: 16150, hgt: 3782}), "/pano/7");
    assert.strictEqual(SITE.panoUrl({i: 7, w: 6400, hgt: 2000}), "/pano/7");
    assert.strictEqual(SITE.panoUrl({i: 7, w: 60000, hgt: 2000}), "/pano/7");
    assert.strictEqual(SITE.panoUrl({i: 7, w: 2048, hgt: 900}), null, "the preview is all of it");
    assert.strictEqual(SITE.panoUrl({i: 7, w: 2400, hgt: 300}), null, "not enough taller");
    assert.strictEqual(SITE.panoUrl({i: 7, w: 4000, hgt: 2000}), null, "not wide");
    assert.strictEqual(SITE.panoUrl({i: 7, w: 6400, hgt: 2000, mv: 1}), null, "a video");
    var bare = loadSite({mode: "static", data: "data/", capabilities: {static: true}});
    assert.strictEqual(bare.panoUrl({i: "ab12", w: 6400, hgt: 2000, pn: 1}), null);
    var withPanos = loadSite({mode: "static", data: "data/",
                              capabilities: {static: true, panos: true}});
    assert.strictEqual(withPanos.panoUrl({i: "ab12", w: 6400, hgt: 2000, pn: 1}),
                       "panos/ab12.jpg");
    assert.strictEqual(withPanos.panoUrl({i: "ab13", w: 6400, hgt: 2000}), null);
});

/* ---------- The stage on a made-up page -------------------------------------- */

// Just enough of a page for VIEWER.Stage: elements (the stage box has the
// size the test gives it), images that load when the test says so, and
// pointer events. log.images: the images the stage asked for.
function fakePage(width, height) {
    var log = {images: []};

    function Element(tag) {
        var classes = {};
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.style = {};
        this.hidden = false;
        this.listeners = {};
        this.classList = {
            add: function (name) {
                classes[name] = true;
            },
            remove: function (name) {
                delete classes[name];
            },
            toggle: function (name, on) {
                if (on === undefined ? !classes[name] : on) {
                    classes[name] = true;
                } else {
                    delete classes[name];
                }
            },
            contains: function (name) {
                return Boolean(classes[name]);
            }
        };
        this.classes = classes;
    }
    Object.defineProperty(Element.prototype, "className", {
        get: function () {
            return Object.keys(this.classes).join(" ");
        },
        set: function (value) {
            var self = this;
            Object.keys(this.classes).forEach(function (name) {
                delete self.classes[name];
            });
            String(value).split(/\s+/).filter(Boolean).forEach(function (name) {
                self.classes[name] = true;
            });
        }
    });
    Element.prototype.setAttribute = function (name, value) {
        this.attributes[name] = String(value);
    };
    Element.prototype.getAttribute = function (name) {
        if (name === "src" && this.src !== undefined) {
            return this.src;
        }
        return name in this.attributes ? this.attributes[name] : null;
    };
    Element.prototype.removeAttribute = function (name) {
        delete this.attributes[name];
        if (name === "src") {
            this.src = "";
        }
    };
    Element.prototype.appendChild = function (child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    };
    Element.prototype.insertBefore = function (child, before) {
        var at = this.children.indexOf(before);
        child.parentNode = this;
        this.children.splice(at < 0 ? this.children.length : at, 0, child);
        return child;
    };
    Element.prototype.removeChild = function (child) {
        this.children.splice(this.children.indexOf(child), 1);
        child.parentNode = null;
        return child;
    };
    Element.prototype.addEventListener = function (name, listener) {
        (this.listeners[name] = this.listeners[name] || []).push(listener);
    };
    Element.prototype.removeEventListener = function () {};
    // (Selectors of the form ".a, .b, tag": by class or tag name.)
    Element.prototype.closest = function (selectors) {
        var list = selectors.split(",").map(function (text) {
            return text.trim();
        });
        for (var node = this; node; node = node.parentNode) {
            for (var k = 0; k < list.length; k++) {
                var wanted = list[k];
                if (wanted.charAt(0) === "." ? node.classes && node.classes[wanted.slice(1)]
                        : node.tagName === wanted.toUpperCase()) {
                    return node;
                }
            }
        }
        return null;
    };
    Element.prototype.setPointerCapture = function () {};
    Element.prototype.getBoundingClientRect = function () {
        return {left: this.rectLeft || 0, top: 0, width: this.rectWidth || width,
                height: height};
    };
    Element.prototype.fire = function (name, event) {
        event = event || {};
        event.type = name;
        event.target = event.target || this;
        event.preventDefault = function () {
            event.defaultPrevented = true;
        };
        event.stopPropagation = function () {};
        (this.listeners[name] || []).forEach(function (listener) {
            listener(event);
        });
        return event;
    };

    // new Image(): loads when the test calls finish(width, height).
    function FakeImage() {
        Element.call(this, "img");
        this.naturalWidth = 0;
        this.naturalHeight = 0;
        log.images.push(this);
    }
    FakeImage.prototype = Object.create(Element.prototype);
    FakeImage.prototype.finish = function (w, h) {
        if (w) {
            this.naturalWidth = w;
            this.naturalHeight = h;
            this.onload();
        } else {
            this.onerror();
        }
    };

    var window = {
        location: {protocol: "http:"},
        devicePixelRatio: 2,
        addEventListener: function () {},
        removeEventListener: function () {},
        requestAnimationFrame: function () {
            return 1;
        },
        cancelAnimationFrame: function () {},
        setTimeout: function () {
            return 1;
        },
        clearTimeout: function () {}
    };
    var document = {
        createElement: function (tag) {
            return new Element(tag);
        },
        createTextNode: function (text) {
            return {textContent: text};
        }
    };
    var box = new Element("div");
    Object.defineProperty(box, "clientWidth", {get: function () {
        return page.width;
    }});
    Object.defineProperty(box, "clientHeight", {get: function () {
        return page.height;
    }});
    var page = {log: log, window: window, document: document, Image: FakeImage, box: box,
                width: width, height: height};
    return page;
}

// Runs body(VIEWER, page) with a fake page of width x height installed as
// the globals the viewer uses.
function onFakePage(width, height, body) {
    var page = fakePage(width, height);
    var saved = {window: global.window, document: global.document, Image: global.Image};
    global.window = page.window;
    global.document = page.document;
    global.Image = page.Image;
    var file = require.resolve(path.join(STATIC, "viewer.js"));
    delete require.cache[file];
    var viewer = require(file);
    delete require.cache[file];
    try {
        return body(viewer, page);
    } finally {
        Object.keys(saved).forEach(function (name) {
            if (saved[name] === undefined) {
                delete global[name];
            } else {
                global[name] = saved[name];
            }
        });
    }
}

// A 16000 x 4000 panorama (4:1): its preview 2048 x 512, its larger picture 6400 x 1600.
var WIDE = {i: 7, w: 16000, hgt: 4000, t: "A wide view"};
var URLS = {thumb: "/thumb/7", preview: "/preview/7", pano: "/pano/7"};

// The stage of a phone (390 x 792) showing WIDE with its preview loaded.
function phoneStage(viewer, page, options) {
    var states = [];
    var stage = new viewer.Stage(page.box, Object.assign({onState: function (state) {
        states.push(state);
    }}, options || {}));
    stage.show(WIDE, URLS);
    page.log.images[page.log.images.length - 1].finish(2048, 512);
    stage.states = states;
    return stage;
}

function shownWidth(stage) {
    return stage.base.w * stage.zoom;
}

function drag(stage, points, type) {
    var pointer = {pointerId: 1, pointerType: type || "touch", button: 0,
                   target: stage.img, preventDefault: function () {}};
    stage.pointerDown(Object.assign({clientX: points[0][0], clientY: points[0][1]}, pointer));
    points.slice(1).forEach(function (point) {
        stage.pointerMove(Object.assign({clientX: point[0], clientY: point[1]}, pointer));
    });
    var last = points[points.length - 1];
    stage.pointerUp(Object.assign({clientX: last[0], clientY: last[1]}, pointer));
}

test("an upright phone opens a wide picture filling the stage's height, at its middle", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        near(stage.base.h * stage.zoom, 792, "fills the height", 1e-6);
        near(format.panCentre(stage.tx, shownWidth(stage), 390), 0.5, "middle");
        assert.strictEqual(stage.ty, 0);
        assert.ok(page.box.classList.contains("is-filling"));
        // It opens there: not moved, so Reset stays hidden.
        assert.strictEqual(stage.isMoved(), false);
        assert.strictEqual(stage.states[stage.states.length - 1].moved, false);
        // The minimap: at the foot of the stage, the part in view framed.
        var map = stage.minimap;
        assert.ok(map && !map.root.hidden);
        assert.ok(page.box.classList.contains("has-minimap"));
        assert.strictEqual(map.root.getAttribute("role"), "slider");
        assert.strictEqual(map.root.getAttribute("aria-valuenow"), "50");
        assert.strictEqual(map.img.src, "/thumb/7");
        // (34 px high, as wide as the picture's shape makes it; at most 60% of the stage.)
        near(parseFloat(map.frame.style.width), 34 * 4, "its width");
        near(parseFloat(map.frame.style.height), 34, "its height");
        near(parseFloat(map.view.style.width), 100 * 390 / shownWidth(stage), "frame", 1e-6);
    });
});

test("the larger picture replaces the preview once zoomed in, and the view stays", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        var large = page.log.images[page.log.images.length - 1];
        assert.strictEqual(large.src, "/pano/7");
        var before = {tx: stage.tx, base: stage.base.w, zoom: stage.zoom};
        large.finish(6400, 1600);
        assert.strictEqual(stage.img.src, "/pano/7");
        assert.deepStrictEqual(stage.natural, {w: 6400, h: 1600});
        near(stage.base.w, before.base, "same size on the stage");
        near(stage.zoom, before.zoom, "same zoom");
        near(stage.tx, before.tx, "same place");
    });
    // Shown whole (a desktop window), it is not loaded.
    onFakePage(1020, 850, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        assert.strictEqual(stage.zoom, 1);
        assert.ok(stage.minimap === null || stage.minimap.root.hidden);
        assert.deepStrictEqual(page.log.images.map(function (image) {
            return image.src;
        }), ["/preview/7"]);
        // Zoomed in by hand: now it is.
        stage.zoomBy(2);
        assert.strictEqual(page.log.images[page.log.images.length - 1].src, "/pano/7");
        assert.ok(!stage.minimap.root.hidden, "and the minimap shows");
    });
});

test("a phone turned, or the pane opened, keeps the same point in the middle", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        drag(stage, [[300, 400], [250, 400], [200, 400]]);
        var centre = stage.centre;
        assert.ok(centre > 0.5);
        // The sheet at its peek height: the stage is lower.
        page.height = 454;
        stage.layout();
        near(stage.base.h * stage.zoom, 454, "fills the lower stage");
        near(format.panCentre(stage.tx, shownWidth(stage), 390), centre, "same middle", 1e-9);
        // Held sideways: shown whole; upright again: the pan view, still there.
        page.width = 844;
        page.height = 338;
        stage.layout();
        assert.strictEqual(stage.zoom, 1);
        assert.ok(stage.minimap.root.hidden);
        page.width = 390;
        page.height = 792;
        stage.layout();
        near(stage.base.h * stage.zoom, 792, "fills again");
        near(format.panCentre(stage.tx, shownWidth(stage), 390), centre, "same middle", 1e-9);
    });
});

test("a drag pans; a swipe that pushes on beyond the end changes picture", function () {
    onFakePage(390, 792, function (viewer, page) {
        var swipes = [];
        var stage = phoneStage(viewer, page, {onSwipe: function (direction) {
            swipes.push(direction);
        }});
        var start = stage.tx;
        drag(stage, [[300, 400], [260, 400], [220, 400]]);
        near(stage.tx, start - 80, "panned");
        assert.deepStrictEqual(swipes, [], "a long drag from the middle only pans");
        assert.ok(stage.isMoved());
        assert.strictEqual(stage.states[stage.states.length - 1].moved, true);
        // Far to the right, a long drag after another: it stops at the end,
        // no picture change.
        for (var k = 0; k < 20 && format.panEnd(stage.tx, shownWidth(stage), 390) !== 1;
             k++) {
            drag(stage, [[380, 400], [200, 400], [20, 400], [10, 400], [5, 400]]);
        }
        assert.strictEqual(format.panEnd(stage.tx, shownWidth(stage), 390), 1);
        near(stage.tx, 390 - shownWidth(stage), "the right end");
        assert.deepStrictEqual(swipes, []);
        assert.strictEqual(stage.minimap.root.getAttribute("aria-valuetext"),
                           "At the right end");
        // From the end, a short push only follows the finger a little...
        drag(stage, [[300, 400], [280, 400], [260, 400]]);
        assert.deepStrictEqual(swipes, []);
        // ... a longer one goes on to the next picture.
        var pointer = {pointerId: 1, pointerType: "touch", button: 0, target: stage.img};
        stage.pointerDown(Object.assign({clientX: 300, clientY: 400}, pointer));
        stage.pointerMove(Object.assign({clientX: 250, clientY: 400}, pointer));
        stage.pointerMove(Object.assign({clientX: 200, clientY: 402}, pointer));
        // (The picture follows the finger beyond its end.)
        assert.ok(stage.img.style.transform.indexOf("translate(" + (stage.tx - 40)) === 0,
                  stage.img.style.transform);
        stage.pointerUp(Object.assign({clientX: 200, clientY: 402}, pointer));
        assert.deepStrictEqual(swipes, [1]);
        // A mouse drag never changes picture.
        drag(stage, [[300, 400], [200, 400], [100, 400]], "mouse");
        assert.deepStrictEqual(swipes, [1]);
    });
});

test("at the left end a swipe to the right goes back; a near end counts as the end", function () {
    onFakePage(390, 792, function (viewer, page) {
        var swipes = [];
        var stage = phoneStage(viewer, page, {onSwipe: function (direction) {
            swipes.push(direction);
        }});
        // Within PAN_END_SLACK of the left end.
        stage.panTo(0);
        stage.panBy(-10);
        assert.strictEqual(format.panEnd(stage.tx, shownWidth(stage), 390), 0);
        drag(stage, [[100, 400], [150, 400], [200, 400]]);
        assert.deepStrictEqual(swipes, [-1]);
    });
});

test("a double tap shows the whole picture, and the pan view again where tapped", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        stage.toggleZoom(195, 400);
        assert.strictEqual(stage.zoom, 1);
        assert.strictEqual(stage.filling, false);
        assert.ok(stage.minimap.root.hidden);
        assert.ok(stage.isMoved(), "Reset brings the pan view back");
        // Tapped a quarter of the way across the whole picture.
        stage.toggleZoom(390 / 4, 400);
        assert.ok(stage.filling);
        near(stage.base.h * stage.zoom, 792, "fills again");
        near(stage.centre, 0.25, "there", 1e-9);
        // Reset: the middle.
        stage.reset();
        near(format.panCentre(stage.tx, shownWidth(stage), 390), 0.5, "middle");
        assert.strictEqual(stage.isMoved(), false);
    });
});

test("zooming by hand leaves the pan view; the minimap and its keys move the view", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        page.log.images[page.log.images.length - 1].finish(6400, 1600);
        var fill = stage.zoom;
        stage.zoomBy(1.5);
        assert.strictEqual(stage.filling, false);
        near(stage.zoom, fill * 1.5, "deeper (a wide picture zooms past its pan view)");
        // (The minimap frames part of its height too now.)
        assert.ok(parseFloat(stage.minimap.view.style.height) < 100);
        stage.reset();
        var root = stage.minimap.root;
        // Home and End: the ends; the arrows a quarter of the stage.
        assert.ok(stage.minimapKey("Home"));
        assert.strictEqual(stage.tx, 0);
        assert.strictEqual(root.getAttribute("aria-valuetext"), "At the left end");
        assert.ok(stage.minimapKey("ArrowRight"));
        near(stage.tx, -390 / 4, "a quarter of the stage");
        assert.ok(stage.minimapKey("End"));
        assert.strictEqual(root.getAttribute("aria-valuenow"), "100");
        assert.strictEqual(stage.minimapKey("ArrowUp"), false);
        assert.strictEqual(stage.minimapKey("x"), false);
        // Its own keydown listener takes the keys it uses.
        var event = root.fire("keydown", {key: "Home"});
        assert.ok(event.defaultPrevented);
        assert.strictEqual(stage.tx, 0);
        assert.ok(!root.fire("keydown", {key: "Enter"}).defaultPrevented);
        // A tap on the minimap moves the view there.
        stage.minimap.frame.rectLeft = 100;
        stage.minimap.frame.rectWidth = 200;
        root.fire("pointerdown", {pointerId: 3, pointerType: "touch", button: 0,
                                  clientX: 150});
        near(format.panCentre(stage.tx, shownWidth(stage), 390), 0.25, "a quarter across", 1e-9);
        root.fire("pointermove", {pointerId: 3, clientX: 300});
        assert.strictEqual(format.panEnd(stage.tx, shownWidth(stage), 390), 1);
        root.fire("pointerup", {pointerId: 3});
        // The stage itself leaves the minimap's pointers alone.
        var before = stage.tx;
        stage.pointerDown({pointerId: 4, pointerType: "touch", button: 0, target: stage.minimap.img,
                           clientX: 10, clientY: 10});
        assert.strictEqual(stage.gesture, null);
        assert.strictEqual(stage.tx, before);
    });
});

test("a sideways scroll pans; on a page the wheel still scrolls past the pan view", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page, {scrollPage: true});
        var start = stage.tx;
        var event = {deltaX: 30, deltaY: 2, deltaMode: 0, ctrlKey: false, clientX: 100,
                     clientY: 100, preventDefault: function () {
                         event.prevented = true;
                     }};
        stage.wheel(event);
        assert.ok(event.prevented);
        near(stage.tx, start - 30, "panned");
        var down = {deltaX: 0, deltaY: 100, deltaMode: 0, ctrlKey: false, clientX: 100,
                    clientY: 100, preventDefault: function () {
                        down.prevented = true;
                    }};
        var zoom = stage.zoom;
        stage.wheel(down);
        assert.ok(!down.prevented, "the page scrolls");
        assert.strictEqual(stage.zoom, zoom);
    });
});

test("ordinary pictures, 360 photos and videos never pan", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = new viewer.Stage(page.box, {});
        stage.show({i: 1, w: 4000, hgt: 3000}, {thumb: "/thumb/1", preview: "/preview/1"});
        page.log.images[0].finish(2048, 1536);
        assert.strictEqual(stage.zoom, 1);
        assert.strictEqual(stage.wide, false);
        assert.ok(!page.box.classList.contains("has-minimap"));
        assert.strictEqual(stage.isMoved(), false);
        // A wide video's poster is shown whole.
        stage.show({i: 2, w: 1920, hgt: 800, mv: 1, vc: "h264"},
                   {thumb: "/thumb/2", preview: "/preview/2", video: null});
        assert.strictEqual(stage.wide, false);
        assert.strictEqual(stage.zoom, 1);
        assert.ok(!page.box.classList.contains("has-minimap"));
    });
});

// The images the stage asked for, by address.
function loading(page, src) {
    return page.log.images.filter(function (image) {
        return image.src === src;
    })[0];
}

test("the pan view keeps its zoom while the preview and the larger picture arrive", function () {
    var site = loadSite({mode: "server", api: "/api", page: "search"});
    onFakePage(390, 792, function (viewer, page) {
        // An 8:1 panorama: its preview (2048 x 256) has too few pixels to fill
        // the stage's height; its larger picture (12800 x 1600) has them.
        page.window.SITE = site;
        var rec = {i: 8, w: 16000, hgt: 2000, t: "Very wide"};
        var stage = new viewer.Stage(page.box, {});
        stage.show(rec, {thumb: "/thumb/8", preview: "/preview/8", pano: "/pano/8"});
        var opened = stage.zoom;
        near(stage.base.h * opened, 792, "the thumbnail fills the height");
        loading(page, "/preview/8").finish(2048, 256);
        near(stage.zoom, opened, "the preview: the same zoom");
        near(stage.base.h * stage.zoom, 792, "still filling");
        loading(page, "/pano/8").finish(12800, 1600);
        near(stage.zoom, opened, "the larger picture: the same zoom");
        // A larger picture that cannot be read leaves the preview's pixels:
        // a band twice their size.
        stage.show(rec, {thumb: "/thumb/8", preview: "/preview/8", pano: "/pano/8"});
        page.log.images[page.log.images.length - 1].finish(2048, 256);
        page.log.images[page.log.images.length - 1].finish(0, 0);
        near(stage.zoom, 2 * 2048 / 390, "the preview's own");
        // One without a larger picture (only a little larger than its
        // preview) opens as it stays: by its preview's pixels.
        var small = {i: 9, w: 2400, hgt: 300, t: "Low"};
        stage.show(small, {thumb: "/thumb/9", preview: "/preview/9", pano: null});
        opened = stage.zoom;
        near(opened, 2 * 2048 / 390, "the preview's");
        loading(page, "/preview/9").finish(2048, 256);
        near(stage.zoom, opened, "the same");
    });
});

test("with only a thumbnail a wide picture is shown whole", function () {
    onFakePage(390, 792, function (viewer, page) {
        // (An export without pictures: thumbnails only.)
        var stage = new viewer.Stage(page.box, {});
        stage.show(WIDE, {thumb: "thumbs/7.jpg", preview: null, pano: null});
        assert.strictEqual(stage.zoom, 1);
        assert.strictEqual(stage.wide, false);
        assert.ok(!page.box.classList.contains("has-minimap"));
        assert.strictEqual(stage.isMoved(), false);
        // A preview that cannot be read: the same, once it is known.
        stage.show(WIDE, URLS);
        assert.ok(stage.zoom > 1, "the pan view while the preview comes");
        loading(page, "/preview/7").finish(0, 0);
        assert.strictEqual(stage.zoom, 1);
        assert.strictEqual(stage.wide, false);
        assert.ok(stage.minimap === null || stage.minimap.root.hidden);
    });
});

test("Reset follows a stage resized into or out of the pan view", function () {
    onFakePage(390, 792, function (viewer, page) {
        var stage = phoneStage(viewer, page);
        drag(stage, [[300, 400], [250, 400], [200, 400]]);
        assert.strictEqual(stage.states[stage.states.length - 1].moved, true);
        // Held sideways: shown whole, as it opens there.
        page.width = 844;
        page.height = 338;
        stage.layout();
        assert.strictEqual(stage.zoom, 1);
        assert.strictEqual(stage.states[stage.states.length - 1].moved, false);
        // Upright again: the pan view where it was, away from its middle.
        page.width = 390;
        page.height = 792;
        stage.layout();
        assert.strictEqual(stage.states[stage.states.length - 1].moved, true);
    });
});

test("on a landscape stage only a picture with the pixels for it pans", function () {
    var site = loadSite({mode: "server", api: "/api", page: "search"});
    // (A 10.7:1 panorama 240 px high, and an 8:1 one 2000 px high.)
    var low = {i: 10, w: 2560, hgt: 240, t: "Low"};
    var high = {i: 11, w: 16000, hgt: 2000, t: "High"};
    function opened(width, height, rec) {
        return onFakePage(width, height, function (viewer, page) {
            page.window.SITE = site;
            var stage = new viewer.Stage(page.box, {});
            stage.show(rec, {thumb: "/thumb/" + rec.i, preview: "/preview/" + rec.i,
                             pano: site.panoUrl(rec)});
            return stage.zoom;
        });
    }
    // A desktop window: the sharp strip of the low one (1400 x 131), the pan
    // view of the high one.
    assert.strictEqual(opened(1400, 850, low), 1);
    near(opened(1400, 850, high) * 1400 / 8, 850, "fills the height");
    // An upright phone pans both (the low one as a band twice its pixels).
    near(opened(390, 792, low), 2 * 2560 / 390, "a band");
    near(opened(390, 792, high) * 390 / 8, 792, "fills the height");
    // (Pure: with too few pixels for a landscape stage, 0.)
    assert.strictEqual(format.panFill(1400, 131, 1400, 850, 240), 0);
    assert.ok(format.panFill(1400, 131, 1400, 850, 600) > 1);
    assert.ok(format.panFill(390, 36, 390, 792, 240) > 1);
});

/* ---------- Runner ------------------------------------------------------------- */

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
