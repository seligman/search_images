/* The map page: the located pictures of the current search as pins on a
   world map, a list of the pictures of a pin, and a way back to the grid
   with just those pictures.

   Coordinates
     - lat/lon: WGS84 degrees.
     - world pixels at zoom z: the equirectangular projection, the world
       (tile * 2^(z+1)) wide and half as tall; x grows east from the date
       line (lon -180), y south from the north pole. The world repeats
       sideways: x and x + width are the same place.
     - screen pixels: a place in the canvas.

   Pins are grouped (clustered) in world pixels, once per zoom level and
   search: the groups do not depend on where the map is panned to, so they
   never change while it is dragged. A group is the pictures of one 64 px
   cell of the world, and groups whose pins would touch are merged (also
   across the date line), so pins never overlap. Each pin is drawn on every
   copy of the world that is on the screen.

   Zoom levels are whole numbers. Past the most detailed map tiles the map
   zooms on with those tiles enlarged (OVERZOOM levels, and at least to
   zoom 10, about 75 m a pixel; drawn fainter the further they are
   stretched, with a note), so pictures taken close together still come
   apart; while a tile loads, a coarser one that is
   already there stands in for it.

   The address holds the search (as on the search page) and the map's
   view ("view=lat,lon,zoom"), so a reload, a link or "Back to the map"
   shows the same place (a view that shows none of the search's pictures
   is not kept: the map fits them), and the open list ("pin=lat,lon").
   The search page keeps the view only while its search stays the same.

   MAP holds the parts without a page (projection, clustering, the wheel,
   fitting a view), also run by the tests under Node
   (tests/js/map_test.js). Pages are built with DOM calls and textContent
   only (never innerHTML).
*/
var MAP = (function () {
    "use strict";

    var hasDocument = typeof document !== "undefined" && document !== null;

    // Pictures are grouped by cells of this many world pixels (a power of
    // two, so the cells fit the world exactly at every zoom).
    var CELL = 64;
    // Pins of different groups keep at least this far apart (pixels).
    var GAP = 3;
    var SINGLE_RADIUS = 7;
    var MAX_RADIUS = 20;
    // Wheel: this many pixels of a mouse wheel make one zoom step (one
    // notch).
    var WHEEL_STEP = 100;
    var WHEEL_LINE = 40;
    var WHEEL_PAGE = 600;
    // A touchpad (or a wheel that scrolls smoothly) sends a stream of
    // steps: less than WHEEL_STREAM ms apart, of less than WHEEL_SMALL
    // pixels, or of changing sizes (a mouse's notches are all alike, or
    // whole multiples of one). Then TOUCHPAD_STEP pixels make one zoom
    // step, and after a step the scrolling of the next TOUCHPAD_REST ms is
    // left out (the momentum of a flick goes on for a while).
    var WHEEL_SMALL = 30;
    var WHEEL_STREAM = 25;
    var TOUCHPAD_STEP = 250;
    var TOUCHPAD_REST = 250;
    // A touchpad pinch arrives as small wheel steps with Ctrl held.
    var PINCH_FACTOR = 3;
    // A pause this long (ms) starts a new wheel gesture.
    var WHEEL_IDLE = 400;

    /* ---------- Projection ------------------------------------------------ */

    function worldWidth(tile, zoom) {
        return tile * Math.pow(2, zoom + 1);
    }

    // x into [0, width).
    function wrapX(x, width) {
        var value = x % width;
        return value < 0 ? value + width : value;
    }

    // A longitude in [-180, 180).
    function normalizeLon(lon) {
        return lon >= -180 && lon < 180 ? lon : wrapX(lon + 180, 360) - 180;
    }

    function validPoint(point) {
        return point !== null && typeof point === "object" &&
            typeof point.lat === "number" && typeof point.lon === "number" &&
            isFinite(point.lat) && isFinite(point.lon) &&
            point.lat >= -90 && point.lat <= 90 && point.lon >= -180 && point.lon <= 180;
    }

    // World pixels (x in [0, width)) of a place.
    function project(lat, lon, width) {
        return {x: wrapX((lon + 180) / 360 * width, width), y: (90 - lat) / 180 * (width / 2)};
    }

    function unproject(x, y, width) {
        return {lat: 90 - y * 180 / (width / 2), lon: normalizeLon(x * 360 / width - 180)};
    }

    // The copy of x (x + k * width) nearest to ref.
    function nearestCopy(x, ref, width) {
        return x + Math.round((ref - x) / width) * width;
    }

    /* The screen x of every copy of world x in a view whose left edge is
       world x = left and that is span pixels wide (margin: also copies
       that far off the screen, for pins that reach into it). */
    function copies(x, left, span, width, margin) {
        var out = [];
        if (!(width > 0)) {
            return out;
        }
        margin = margin || 0;
        var first = x + Math.ceil((left - margin - x) / width) * width;
        for (var at = first; at <= left + span + margin; at += width) {
            out.push(at - left);
        }
        return out;
    }

    /* ---------- Groups of pictures (clusters) ---------------------------- */

    function pinRadius(count) {
        return count <= 1 ? SINGLE_RADIUS
            : Math.min(MAX_RADIUS, Math.round(9 + 2.5 * Math.log(count)));
    }

    // "1,234" up to 9,999, then "12k".
    function countLabel(count) {
        if (count >= 10000) {
            return Math.floor(count / 1000) + "k";
        }
        return count >= 1000 ? Math.floor(count / 1000) + "," + String(count % 1000 + 1000).slice(1)
            : String(count);
    }

    /* Merges groups whose pins would touch until none do: the larger group
       takes the smaller, at their weighted middle (the short way round the
       world), and its pin grows. */
    function mergeTouching(groups, width, gap) {
        var size = CELL;
        var cols = Math.max(1, Math.round(width / size));
        var changed = true;
        while (changed) {
            changed = false;
            var buckets = {};
            groups.forEach(function (group, index) {
                group.dead = false;
                var key = (Math.floor(group.x / size) % cols) + ":" + Math.floor(group.y / size);
                (buckets[key] = buckets[key] || []).push(index);
            });
            var order = groups.map(function (group, index) {
                return index;
            }).sort(function (a, b) {
                var ga = groups[a];
                var gb = groups[b];
                return gb.count - ga.count || ga.y - gb.y || ga.x - gb.x;
            });
            for (var k = 0; k < order.length; k++) {
                var group = groups[order[k]];
                if (group.dead) {
                    continue;
                }
                var bx = Math.floor(group.x / size) % cols;
                var by = Math.floor(group.y / size);
                var seen = {};
                for (var dx = -1; dx <= 1; dx++) {
                    for (var dy = -1; dy <= 1; dy++) {
                        var key = ((bx + dx + cols) % cols) + ":" + (by + dy);
                        if (seen[key] || !buckets[key]) {
                            continue;
                        }
                        seen[key] = true;
                        var list = buckets[key];
                        for (var m = 0; m < list.length; m++) {
                            var other = groups[list[m]];
                            if (other === group || other.dead) {
                                continue;
                            }
                            var ox = nearestCopy(other.x, group.x, width);
                            var ddx = ox - group.x;
                            var ddy = other.y - group.y;
                            var reach = group.r + other.r + gap;
                            if (ddx * ddx + ddy * ddy < reach * reach) {
                                var total = group.count + other.count;
                                group.x = wrapX((group.x * group.count + ox * other.count) / total,
                                                width);
                                group.y = (group.y * group.count + other.y * other.count) / total;
                                group.count = total;
                                group.ids = group.ids.concat(other.ids);
                                group.r = pinRadius(total);
                                other.dead = true;
                                changed = true;
                            }
                        }
                    }
                }
            }
            groups = groups.filter(function (item) {
                return !item.dead;
            });
        }
        groups.forEach(function (item) {
            delete item.dead;
        });
        return groups;
    }

    /* The pins of points ([{id, lat, lon}]) on a world width pixels wide:
       [{x, y, count, ids, r}] in world pixels (x in [0, width)), ordered
       top to bottom. The same points and width always give the same pins,
       wherever the map is panned to. options: cell, gap (pixels). */
    function cluster(points, width, options) {
        options = options || {};
        var cell = options.cell || CELL;
        var gap = options.gap === undefined ? GAP : options.gap;
        var cols = Math.max(1, Math.round(width / cell));
        var cells = {};
        var groups = [];
        (points || []).forEach(function (point) {
            if (!validPoint(point)) {
                return;
            }
            var at = project(point.lat, point.lon, width);
            var key = (Math.floor(at.x / cell) % cols) + ":" + Math.floor(at.y / cell);
            var group = cells[key];
            if (!group) {
                group = {sx: 0, sy: 0, ref: at.x, ids: []};
                cells[key] = group;
                groups.push(group);
            }
            group.sx += nearestCopy(at.x, group.ref, width);
            group.sy += at.y;
            group.ids.push(point.id);
        });
        groups = groups.map(function (group) {
            var count = group.ids.length;
            return {x: wrapX(group.sx / count, width), y: group.sy / count, count: count,
                    ids: group.ids, r: pinRadius(count)};
        });
        groups = mergeTouching(groups, width, gap);
        groups.sort(function (a, b) {
            return a.y - b.y || a.x - b.x;
        });
        return groups;
    }

    /* The pins on the screen: every copy of every group (cluster()) that
       is (partly) in a view whose top left corner is world (left, top),
       viewW x viewH pixels, of a world width pixels wide: [{sx, sy, r,
       group}], larger groups last (drawn on top). */
    function screenPins(groups, left, top, viewW, viewH, width) {
        var list = [];
        groups.forEach(function (group) {
            var sy = group.y - top;
            var margin = group.r + 6;
            if (sy < -margin || sy > viewH + margin) {
                return;
            }
            copies(group.x, left, viewW, width, margin).forEach(function (sx) {
                list.push({sx: sx, sy: sy, r: group.r, group: group});
            });
        });
        list.sort(function (a, b) {
            return a.group.count - b.group.count;
        });
        return list;
    }

    /* The pin under a point of the screen: pins are [{sx, sy, r}] in the
       order they are drawn; the one whose edge is nearest wins, and of
       equals the one drawn last (on top). slack: pixels around a pin that
       still count (a finger is less precise than a mouse). */
    function hitPin(pins, x, y, slack) {
        var best = null;
        var bestScore = Infinity;
        slack = slack || 0;
        for (var k = 0; k < pins.length; k++) {
            var pin = pins[k];
            var dx = pin.sx - x;
            var dy = pin.sy - y;
            var distance = Math.sqrt(dx * dx + dy * dy);
            if (distance <= pin.r + slack && distance - pin.r <= bestScore) {
                best = pin;
                bestScore = distance - pin.r;
            }
        }
        return best;
    }

    /* ---------- The wheel ---------------------------------------------- */

    /* Wheel events to zoom steps. Only the up/down part zooms: a sideways
       scroll (a touchpad swipe, a tilt wheel, Shift+wheel) pans instead.
       Steps add up: WHEEL_STEP pixels of a mouse wheel (one notch) make one
       zoom level. A gesture (events without a pause) that turns out to be
       a stream of small steps (a touchpad) needs TOUCHPAD_STEP pixels a
       level and rests after each, so a flick and its momentum zoom a
       level or two, not ten. A pinch (Ctrl) zooms at its own pace.
       feed(event) -> {steps: 1 (in), -1 (out) or 0, panX: pixels to pan
       sideways}. */
    function WheelZoom(options) {
        options = options || {};
        this.step = options.step || WHEEL_STEP;
        this.idle = options.idle || WHEEL_IDLE;
        this.amount = 0;
        this.last = -Infinity;
        this.lastStep = -Infinity;
        this.lastSize = 0;
        this.touchpad = false;
    }

    // a and b (> 0) are the same size, or one a whole multiple of the other.
    function alike(a, b) {
        var ratio = Math.max(a, b) / Math.min(a, b);
        return Math.abs(ratio - Math.round(ratio)) < 0.02;
    }

    WheelZoom.prototype.feed = function (event) {
        var unit = event.deltaMode === 1 ? WHEEL_LINE : event.deltaMode === 2 ? WHEEL_PAGE : 1;
        var dy = (Number(event.deltaY) || 0) * unit;
        var dx = (Number(event.deltaX) || 0) * unit;
        var now = typeof event.timeStamp === "number" ? event.timeStamp : Date.now();
        var out = {steps: 0, panX: 0};
        if (Math.abs(dx) > Math.abs(dy)) {
            out.panX = -dx;
            return out;
        }
        if (!dy) {
            return out;
        }
        var pinch = Boolean(event.ctrlKey);
        var since = now - this.last;
        if (since > this.idle) {
            this.amount = 0;
            this.lastSize = 0;
            this.touchpad = false;
        } else if (this.amount !== 0 && (this.amount > 0) !== (dy > 0)) {
            this.amount = 0;
        }
        this.last = now;
        var size = Math.abs(dy);
        if (pinch) {
            dy *= PINCH_FACTOR;
        } else if (unit === 1 && (size < WHEEL_SMALL || since < WHEEL_STREAM ||
                                  (this.lastSize && !alike(size, this.lastSize)))) {
            this.touchpad = true;
        }
        this.lastSize = unit === 1 && !pinch ? size : 0;
        var step = this.step;
        if (this.touchpad && !pinch) {
            if (now - this.lastStep < TOUCHPAD_REST) {
                return out;
            }
            step = Math.max(step, TOUCHPAD_STEP);
        }
        this.amount += dy;
        if (Math.abs(this.amount) >= step) {
            out.steps = this.amount < 0 ? 1 : -1;
            this.amount = 0;
            this.lastStep = now;
        }
        return out;
    };

    /* ---------- Views ------------------------------------------------------ */

    /* The view {lat, lon, zoom} that shows all points with pad pixels to
       spare: the highest whole zoom in [minZoom, maxZoom] where they fit,
       centred on them. The longitudes are spanned the short way round: the
       widest gap between them is left out, so pictures on both sides of the
       date line are shown together. null without points. */
    function fitView(points, screenW, screenH, tile, minZoom, maxZoom, pad) {
        pad = pad === undefined ? 40 : pad;
        var lons = [];
        var south = 90;
        var north = -90;
        (points || []).forEach(function (point) {
            if (validPoint(point)) {
                lons.push(normalizeLon(point.lon));
                south = Math.min(south, point.lat);
                north = Math.max(north, point.lat);
            }
        });
        if (!lons.length) {
            return null;
        }
        lons.sort(function (a, b) {
            return a - b;
        });
        var gapStart = lons[lons.length - 1];
        var gap = lons[0] + 360 - lons[lons.length - 1];
        for (var k = 1; k < lons.length; k++) {
            if (lons[k] - lons[k - 1] > gap) {
                gap = lons[k] - lons[k - 1];
                gapStart = lons[k - 1];
            }
        }
        var span = 360 - gap;
        var west = gapStart + gap;
        var zoom = minZoom;
        for (var z = minZoom; z <= maxZoom; z++) {
            var width = worldWidth(tile, z);
            if (span / 360 * width <= Math.max(1, screenW - 2 * pad) &&
                    (north - south) / 180 * (width / 2) <= Math.max(1, screenH - 2 * pad)) {
                zoom = z;
            } else {
                break;
            }
        }
        return {lat: (south + north) / 2, lon: normalizeLon(west + span / 2), zoom: zoom};
    }

    function shortNumber(value) {
        var text = String(Number(value.toFixed(4)));
        return text === "-0" ? "0" : text;
    }

    // "lat,lon,zoom" for the address (SITE.searchState reads it back).
    function formatView(view) {
        return shortNumber(view.lat) + "," + shortNumber(normalizeLon(view.lon)) + "," +
            Math.round(view.zoom);
    }

    // {lat, lon, zoom} of formatView's text, or null.
    function parseView(text) {
        var match = /^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(\d+)$/.exec(String(text || ""));
        if (!match) {
            return null;
        }
        var lat = Number(match[1]);
        var lon = Number(match[2]);
        if (!(lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)) {
            return null;
        }
        return {lat: lat, lon: normalizeLon(lon), zoom: Number(match[3])};
    }

    /* The open list is in the address too ("pin=lat,lon", the middle of
       its pin), so Back to a page the browser did not keep, or a reload,
       opens it again. */
    function formatPoint(point) {
        function six(value) {
            var text = String(Number(value.toFixed(6)));
            return text === "-0" ? "0" : text;
        }
        return six(point.lat) + "," + six(normalizeLon(point.lon));
    }

    // {lat, lon} of formatPoint's text, or null.
    function parsePoint(text) {
        var match = /^(-?\d{1,2}(?:\.\d{1,8})?),(-?\d{1,3}(?:\.\d{1,8})?)$/.exec(String(text || ""));
        if (!match) {
            return null;
        }
        var point = {lat: Number(match[1]), lon: Number(match[2])};
        return validPoint(point) ? {lat: point.lat, lon: normalizeLon(point.lon)} : null;
    }

    // The group (of cluster()) whose pin covers a place, or null.
    function groupAt(groups, lat, lon, width) {
        var at = project(lat, lon, width);
        var best = null;
        var bestDistance = Infinity;
        (groups || []).forEach(function (group) {
            var dx = nearestCopy(group.x, at.x, width) - at.x;
            var dy = group.y - at.y;
            var distance = Math.sqrt(dx * dx + dy * dy);
            if (distance <= group.r && distance < bestDistance) {
                best = group;
                bestDistance = distance;
            }
        });
        return best;
    }

    /* ---------- The page ------------------------------------------------- */

    function startPage() {
        var el = SHELL.el;
        var clear = SHELL.clear;
        var fmt = SHELL.format;

        var TILE = SITE.tileSize();
        var tilesMax = SITE.tilesMax();
        // The most detailed tiles: the pyramid's, or (without one) the
        // small bundled map the server cuts up to zoom 3.
        var TILE_ZOOM = tilesMax === null ? 3 : Math.max(0, tilesMax);
        var OVERZOOM = 4;
        var MIN_ZOOM = 0;
        var MAX_ZOOM = Math.max(TILE_ZOOM + OVERZOOM, 10);
        var OCEAN = "#1a2c3d";
        var PANEL_PAGE = 120;
        var MOUSE_SLACK = 4;
        var TOUCH_SLACK = 12;
        // (sessionStorage; app.js reads it.)
        var FROM_MAP_KEY = "searchImages.fromMap";

        var canvas = document.getElementById("map-canvas");
        var ctx = canvas.getContext("2d");
        var panel = document.getElementById("map-panel");
        var panelTitle = document.getElementById("map-panel-title");
        var panelList = document.getElementById("map-panel-list");
        var panelMore = document.getElementById("map-panel-more");
        var gridLink = document.getElementById("map-panel-grid");
        var gridNote = document.getElementById("map-panel-note");
        var statusBar = document.getElementById("map-status");
        var messageBox = document.getElementById("map-message");
        var detailNote = document.getElementById("map-note");
        var targetNote = document.getElementById("map-target");

        var search = SITE.searchState.read();
        var adminOn = Boolean(SITE.capabilities.admin) && !SITE.capabilities.static;
        // (SITE.searchState.read has checked the value already.)
        var startView = parseView(search.view);
        // The view is still the one from the address (until the pictures
        // are there).
        var viewFromAddress = Boolean(startView);
        // The list that was open (see formatPoint), opened again once the
        // pictures are there.
        var startPin = parsePoint(new URLSearchParams(window.location.search).get("pin"));

        var view = {lat: 20, lon: 0, zoom: 1, w: 0, h: 0};
        var dpr = 0;
        var ratioQuery = null;
        var tileCache = {};
        var records = [];
        var indexById = {};
        var indexLibraries = [];
        var searchIndex = null;
        var allLocations = null;
        var locations = [];
        var matchCount = 0;
        var rank = {};
        var loadState = "loading";      // loading | failed | ready
        var placed = false;              // the view has been set (address or fit)
        var groupsCache = {zoom: -1, version: -1, groups: []};
        var locationsVersion = 0;
        var pins = [];                   // the pins drawn last: [{sx, sy, r, group}]
        var selection = null;            // the pictures of the open list
        var wheel = new WheelZoom();
        var viewerPushed = false;
        // How the map was last used while it had the focus: "key",
        // "pointer" (a mouse or a finger) or "" (not since it got it).
        var usedBy = "";

        /* View */

        function width() {
            return worldWidth(TILE, view.zoom);
        }

        function clampView() {
            var w = width();
            var top = view.h / 2;
            var bottom = w / 2 - view.h / 2;
            if (bottom < top) {
                view.lat = 0;
            } else {
                var y = (90 - view.lat) / 180 * (w / 2);
                y = Math.max(top, Math.min(bottom, y));
                view.lat = 90 - y * 180 / (w / 2);
            }
            view.lon = normalizeLon(view.lon);
        }

        // World x and y of the screen's top left corner.
        function corner() {
            var w = width();
            var centre = project(view.lat, view.lon, w);
            return {left: centre.x - view.w / 2, top: centre.y - view.h / 2, w: w};
        }

        function setView(lat, lon, zoom) {
            view.lat = lat;
            view.lon = lon;
            view.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(zoom)));
            clampView();
            changedView();
        }

        // The world filling the screen (the view without pictures).
        function worldView() {
            var zoom = MIN_ZOOM;
            while (zoom < TILE_ZOOM && (worldWidth(TILE, zoom) < view.w ||
                                        worldWidth(TILE, zoom) / 2 < view.h)) {
                zoom += 1;
            }
            setView(20, 0, zoom);
        }

        function zoomAt(sx, sy, zoom) {
            zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(zoom)));
            if (zoom === view.zoom) {
                return;
            }
            var at = corner();
            var anchor = unproject(at.left + sx, at.top + sy, at.w);
            var w = worldWidth(TILE, zoom);
            var spot = project(anchor.lat, anchor.lon, w);
            view.zoom = zoom;
            var centre = unproject(spot.x + view.w / 2 - sx, spot.y + view.h / 2 - sy, w);
            view.lat = centre.lat;
            view.lon = centre.lon;
            clampView();
            changedView();
        }

        function panBy(dx, dy) {
            var w = width();
            view.lon -= dx * 360 / w;
            view.lat += dy * 180 / (w / 2);
            clampView();
            changedView();
        }

        var viewTimer = 0;

        function changedView() {
            placed = true;
            scheduleDraw();
            window.clearTimeout(viewTimer);
            viewTimer = window.setTimeout(function () {
                if (!VIEWER.isOpen()) {
                    SHELL.setState({view: formatView(view), ids: null});
                }
                updateLinks();
            }, 250);
        }

        /* Size of the canvas */

        function onRatioChange() {
            resize();
        }

        // Redraws when the window moves to a screen of another pixel density
        // (or the page is zoomed): the canvas then needs a new size.
        function watchRatio() {
            if (ratioQuery) {
                if (ratioQuery.removeEventListener) {
                    ratioQuery.removeEventListener("change", onRatioChange);
                } else if (ratioQuery.removeListener) {
                    ratioQuery.removeListener(onRatioChange);
                }
                ratioQuery = null;
            }
            if (!window.matchMedia) {
                return;
            }
            ratioQuery = window.matchMedia("(resolution: " + dpr + "dppx)");
            if (ratioQuery.addEventListener) {
                ratioQuery.addEventListener("change", onRatioChange);
            } else if (ratioQuery.addListener) {
                ratioQuery.addListener(onRatioChange);
            }
        }

        function resize() {
            var rect = canvas.getBoundingClientRect();
            var ratio = window.devicePixelRatio || 1;
            view.w = rect.width;
            view.h = rect.height;
            var backW = Math.max(1, Math.round(rect.width * ratio));
            var backH = Math.max(1, Math.round(rect.height * ratio));
            if (canvas.width !== backW || canvas.height !== backH) {
                canvas.width = backW;
                canvas.height = backH;
            }
            if (ratio !== dpr) {
                dpr = ratio;
                watchRatio();
            }
            clampView();
            scheduleDraw();
        }

        /* Tiles */

        function tileKey(z, x, y) {
            return z + "/" + x + "/" + y;
        }

        function getTile(z, x, y) {
            var key = tileKey(z, x, y);
            if (tileCache[key]) {
                return tileCache[key];
            }
            var url = SITE.tileUrl(z, x, y);
            if (!url) {
                return null;
            }
            var img = new Image();
            var entry = {img: img, loaded: false, failed: false};
            tileCache[key] = entry;
            img.onload = function () {
                entry.loaded = true;
                scheduleDraw();
            };
            img.onerror = function () {
                entry.failed = true;
            };
            img.src = url;
            return entry;
        }

        // Part of a coarser tile that is already loaded, in place of one that is not.
        function drawFromParent(z, x, y, dx, dy, dw, dh) {
            for (var up = 1; up <= z; up++) {
                var n = Math.pow(2, up);
                var entry = tileCache[tileKey(z - up, Math.floor(x / n), Math.floor(y / n))];
                if (entry && entry.loaded) {
                    var part = TILE / n;
                    ctx.drawImage(entry.img, (x % n) * part, (y % n) * part, part, part,
                                  dx, dy, dw, dh);
                    return true;
                }
            }
            return false;
        }

        function drawTiles(at) {
            var tz = Math.min(view.zoom, TILE_ZOOM);
            var size = TILE * Math.pow(2, view.zoom - tz);
            // Tiles stretched far past their detail are only a hint of
            // land and sea: drawn fainter, over the plain sea colour.
            var stretch = view.zoom - tz;
            ctx.globalAlpha = stretch <= 1 ? 1 : Math.max(0.35, 1 - 0.2 * (stretch - 1));
            detailNote.hidden = stretch < 2;
            var cols = Math.pow(2, tz + 1);
            var rows = Math.pow(2, tz);
            var x0 = Math.floor(at.left / size);
            var x1 = Math.floor((at.left + view.w) / size);
            var y0 = Math.max(0, Math.floor(at.top / size));
            var y1 = Math.min(rows - 1, Math.floor((at.top + view.h) / size));
            for (var ty = y0; ty <= y1; ty++) {
                var dy = Math.round(ty * size - at.top);
                var dh = Math.round((ty + 1) * size - at.top) - dy;
                for (var tx = x0; tx <= x1; tx++) {
                    // The world repeats sideways: column tx is column tx mod cols.
                    var x = ((tx % cols) + cols) % cols;
                    var dx = Math.round(tx * size - at.left);
                    var dw = Math.round((tx + 1) * size - at.left) - dx;
                    var entry = getTile(tz, x, ty);
                    if (entry && entry.loaded) {
                        ctx.drawImage(entry.img, dx, dy, dw, dh);
                    } else if (!drawFromParent(tz, x, ty, dx, dy, dw, dh)) {
                        ctx.fillStyle = OCEAN;
                        ctx.fillRect(dx, dy, dw, dh);
                    }
                }
            }
            ctx.globalAlpha = 1;
            // Keep the cache bounded in long sessions (oldest first; the
            // coarse levels stay, they stand in for missing tiles).
            var keys = Object.keys(tileCache);
            if (keys.length > 500) {
                for (var k = 0; k < keys.length - 300; k++) {
                    if (Number(keys[k].split("/")[0]) > 2) {
                        delete tileCache[keys[k]];
                    }
                }
            }
        }

        /* Pins */

        function groupsNow() {
            if (groupsCache.zoom !== view.zoom || groupsCache.version !== locationsVersion) {
                groupsCache = {zoom: view.zoom, version: locationsVersion,
                               groups: cluster(locations, width())};
                markPicked();
            }
            return groupsCache.groups;
        }

        function markPicked() {
            groupsCache.groups.forEach(function (group) {
                group.picked = Boolean(selection) && group.ids.some(function (id) {
                    return selection.picked[id] === true;
                });
            });
        }

        function drawPins(at) {
            var list = screenPins(groupsNow(), at.left, at.top, view.w, view.h, at.w);
            ctx.save();
            ctx.font = "600 12px system-ui, -apple-system, \"Segoe UI\", sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            list.forEach(function (pin) {
                var group = pin.group;
                if (group.picked) {
                    ctx.beginPath();
                    ctx.arc(pin.sx, pin.sy, pin.r + 4, 0, Math.PI * 2);
                    ctx.lineWidth = 3;
                    ctx.strokeStyle = "rgba(255, 210, 63, 0.95)";
                    ctx.stroke();
                }
                ctx.beginPath();
                ctx.arc(pin.sx, pin.sy, pin.r, 0, Math.PI * 2);
                ctx.fillStyle = group.count === 1 ? "rgba(214, 58, 58, 0.95)"
                    : "rgba(31, 103, 214, 0.95)";
                ctx.fill();
                ctx.lineWidth = 2;
                ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
                ctx.stroke();
                if (group.count > 1) {
                    ctx.fillStyle = "#ffffff";
                    ctx.fillText(countLabel(group.count), pin.sx, pin.sy + 0.5);
                }
            });
            ctx.restore();
            pins = list;
        }

        var dirty = false;

        function scheduleDraw() {
            if (dirty) {
                return;
            }
            dirty = true;
            window.requestAnimationFrame(draw);
        }

        function draw() {
            dirty = false;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = OCEAN;
            ctx.fillRect(0, 0, view.w, view.h);
            var at = corner();
            drawTiles(at);
            drawPins(at);
            drawTarget();
        }

        /* With the keyboard, Enter lists the pictures of the pin nearest
           the middle: while the map has the keyboard's focus, that pin has
           a ring (and its count is read out), and a small cross marks the
           middle. */
        function keyboardFocus() {
            if (document.activeElement !== canvas || usedBy === "pointer") {
                return false;
            }
            if (usedBy === "key") {
                return true;
            }
            try {
                return canvas.matches(":focus-visible");
            } catch (error) {
                return false;                   // (a browser without :focus-visible)
            }
        }

        // (The browser may keep showing the focus ring after a click when
        // the focus had come by keyboard: style.css hides it then.)
        function setUsedBy(value) {
            if (usedBy !== value) {
                usedBy = value;
                canvas.classList.toggle("pointer-used", value === "pointer");
                scheduleDraw();
            }
        }

        function drawTarget() {
            var focused = keyboardFocus();
            var pin = focused ? nearestPin() : null;
            announceTarget(focused, pin);
            if (!focused) {
                return;
            }
            var cx = Math.round(view.w / 2) + 0.5;
            var cy = Math.round(view.h / 2) + 0.5;
            ctx.save();
            ctx.beginPath();
            ctx.moveTo(cx - 7, cy);
            ctx.lineTo(cx + 7, cy);
            ctx.moveTo(cx, cy - 7);
            ctx.lineTo(cx, cy + 7);
            ctx.lineWidth = 3;
            ctx.strokeStyle = "rgba(12, 17, 24, 0.8)";
            ctx.stroke();
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = "#ffffff";
            ctx.stroke();
            if (pin) {
                ctx.beginPath();
                ctx.arc(pin.sx, pin.sy, pin.r + 8, 0, Math.PI * 2);
                ctx.lineWidth = 4;
                ctx.strokeStyle = "rgba(12, 17, 24, 0.85)";
                ctx.stroke();
                ctx.setLineDash([5, 4]);
                ctx.lineWidth = 2;
                ctx.strokeStyle = "#ffffff";
                ctx.stroke();
            }
            ctx.restore();
        }

        // The group last read out (undefined: the map has no keyboard focus).
        var announced;
        var announceTimer = 0;

        function announceTarget(focused, pin) {
            var group = focused ? (pin ? pin.group : null) : undefined;
            if (group === announced) {
                return;
            }
            announced = group;
            window.clearTimeout(announceTimer);
            if (group === undefined) {
                targetNote.textContent = "";
                return;
            }
            announceTimer = window.setTimeout(function () {
                targetNote.textContent = group
                    ? "Nearest pin: " + fmt.plural(group.count, "picture") + ". Enter lists them."
                    : "No pins in sight.";
            }, 400);
        }

        /* Messages */

        function noteButton(text, onClick) {
            return el("button", {type: "button", className: "button primary", text: text,
                                 on: {click: onClick}});
        }

        function showMessage(title, lines, actions) {
            clear(messageBox);
            if (!title) {
                messageBox.hidden = true;
                return;
            }
            messageBox.appendChild(el("h2", {text: title}));
            (lines || []).forEach(function (line) {
                messageBox.appendChild(el("p", {text: line}));
            });
            if (actions && actions.length) {
                messageBox.appendChild(el("div", {className: "map-message-actions"}, actions));
            }
            messageBox.hidden = false;
        }

        function filtered() {
            return Boolean(search.q || Object.keys(search.filters).length || SHELL.library());
        }

        function renderStatus() {
            if (loadState === "loading") {
                statusBar.textContent = "Loading the pictures\u2026";
                showMessage(null);
                return;
            }
            if (loadState === "failed") {
                statusBar.textContent = "";
                showMessage("The pictures could not be loaded",
                            [SITE.isStatic ? "Some files of this site may be missing."
                                : "Is the server still running?"],
                            [noteButton("Try again", load)]);
                return;
            }
            var count = locations.length;
            if (count) {
                statusBar.textContent = filtered() && matchCount > count
                    ? fmt.number(count) + " of " + fmt.plural(matchCount, "matching picture") +
                      " have a location"
                    : fmt.plural(count, "picture") + " with a location";
                showMessage(null);
                return;
            }
            statusBar.textContent = "";
            var back = el("a", {className: "button", href: SITE.searchUrl(searchLinkState()),
                                text: "Back to search"});
            if (filtered() && matchCount) {
                showMessage(matchCount === 1 ? "The picture that matches has no location"
                    : "None of the " + fmt.plural(matchCount, "picture") + " that match " +
                      "have a location",
                            ["Only pictures with a GPS position in their file are on the map."],
                            [back]);
            } else if (filtered()) {
                showMessage("No pictures match this search", [], [back]);
            } else {
                showMessage("No pictures with a location yet",
                            ["Pictures appear on the map when their files hold a GPS position " +
                             "(most phone photos do)."], []);
            }
        }

        /* Links */

        // The search for links back to the search page: this one, without
        // a list of pictures, at this view.
        function searchLinkState() {
            var state = SITE.searchState.read();
            state.lib = SHELL.library();
            state.size = "";
            state.ids = "";
            state.view = formatView(view);
            return state;
        }

        function updateLinks() {
            var url = SITE.searchUrl(searchLinkState());
            document.getElementById("map-back").href = url;
            var searchLink = SHELL.navLink ? SHELL.navLink("search") : null;
            if (searchLink) {
                searchLink.href = url;
            }
            updateGridLink();
        }

        function updateGridLink() {
            if (!selection) {
                return;
            }
            var text = SITE.idList.encode(selection.ids, records);
            gridLink.textContent = selection.ids.length === 1 ? "Show it in the grid"
                : "Show these in the grid";
            if (text && text.length <= SITE.idList.maxLength()) {
                var state = searchLinkState();
                state.ids = text;
                gridLink.href = SITE.searchUrl(state);
                gridLink.removeAttribute("aria-disabled");
                gridLink.classList.remove("is-disabled");
                gridNote.hidden = true;
            } else {
                gridLink.removeAttribute("href");
                gridLink.setAttribute("aria-disabled", "true");
                gridLink.classList.add("is-disabled");
                gridNote.textContent = "Too many pictures for one link: zoom in to pick fewer.";
                gridNote.hidden = false;
            }
        }

        /* The list of a pin's pictures */

        function byRank(a, b) {
            var ra = rank[a] === undefined ? Infinity : rank[a];
            var rb = rank[b] === undefined ? Infinity : rank[b];
            return ra - rb || (String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0);
        }

        function panelItem(id, index) {
            var rec = indexById[id] || {i: id};
            var link = el("a", {className: "map-panel-item",
                                href: SITE.detailUrl(id, searchLinkState())});
            link.setAttribute("data-index", String(index));
            var image = el("img", {alt: "", src: SITE.thumbUrl(rec), loading: "lazy",
                                   draggable: false});
            image.decoding = "async";
            link.appendChild(image);
            var text = el("span", {className: "map-panel-text"});
            text.appendChild(el("span", {className: "map-panel-name",
                                         text: rec.t || rec.n || VIEWER.altText(rec) ||
                                             "Picture"}));
            var date = VIEWER.format.shortDate(rec.dk);
            if (date) {
                text.appendChild(el("span", {className: "map-panel-date", text: date}));
            }
            link.appendChild(text);
            link.addEventListener("click", function (event) {
                if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey ||
                        event.altKey) {
                    return;
                }
                event.preventDefault();
                openViewer(index);
            });
            return el("li", {}, [link]);
        }

        function showMore() {
            var end = Math.min(selection.ids.length, selection.shown + PANEL_PAGE);
            for (var k = selection.shown; k < end; k++) {
                panelList.appendChild(panelItem(selection.ids[k], k));
            }
            selection.shown = end;
            var left = selection.ids.length - end;
            panelMore.hidden = left <= 0;
            panelMore.textContent = "Show " + fmt.number(Math.min(left, PANEL_PAGE)) + " more";
        }

        /* The list covers part of the map (the left side; on a phone the
           foot): a pin it would hide moves into the part still in sight. */
        function keepInSight(group) {
            var at = corner();
            var xs = copies(group.x, at.left, view.w, at.w, 0);
            if (!xs.length) {
                return;
            }
            var sx = xs.reduce(function (best, x) {
                return Math.abs(x - view.w / 2) < Math.abs(best - view.w / 2) ? x : best;
            });
            var sy = group.y - at.top;
            var stage = canvas.getBoundingClientRect();
            var box = panel.getBoundingClientRect();
            var left = box.left - stage.left;
            var right = box.right - stage.left;
            var top = box.top - stage.top;
            var bottom = box.bottom - stage.top;
            var margin = group.r + 12;
            if (sx + margin < left || sx - margin > right || sy + margin < top ||
                    sy - margin > bottom) {
                return;
            }
            var sheet = right - left >= view.w * 0.9 && top > 0;
            var x = sheet ? view.w / 2 : right + (view.w - right) / 2;
            var y = sheet ? top / 2 : view.h / 2;
            panBy(x - sx, y - sy);
        }

        function openPanel(group, byKeyboard) {
            var ids = group.ids.slice().sort(byRank);
            var picked = {};
            ids.forEach(function (id) {
                picked[id] = true;
            });
            selection = {ids: ids, picked: picked, shown: 0};
            markPicked();
            clear(panelList);
            panelTitle.textContent = fmt.plural(ids.length, "picture");
            panelList.scrollTop = 0;
            showMore();
            updateGridLink();
            panel.hidden = false;
            document.body.classList.add("map-panel-open");
            SHELL.setState({pin: formatPoint(unproject(group.x, group.y, width()))});
            keepInSight(group);
            scheduleDraw();
            if (byKeyboard) {
                panelTitle.focus();
            }
        }

        function closePanel(returnFocus) {
            if (panel.hidden) {
                return;
            }
            panel.hidden = true;
            document.body.classList.remove("map-panel-open");
            SHELL.setState({pin: null});
            selection = null;
            markPicked();
            clear(panelList);
            scheduleDraw();
            if (returnFocus) {
                canvas.focus({preventScroll: true});
            }
        }

        panelMore.addEventListener("click", function () {
            if (selection) {
                showMore();
            }
        });
        document.getElementById("map-panel-close").addEventListener("click", function () {
            closePanel(true);
        });
        gridLink.addEventListener("click", function (event) {
            if (gridLink.getAttribute("aria-disabled") === "true") {
                event.preventDefault();
                return;
            }
            if (event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey &&
                    !event.altKey) {
                // Tells the search page it was opened from here, so its "Back
                // to the map" goes back in the history (to the map as it was,
                // its list open). A page opened from disk has no referrer.
                try {
                    window.sessionStorage.setItem(FROM_MAP_KEY, gridLink.href);
                } catch (error) {
                    // (No storage: the search page goes by the referrer.)
                }
            }
        });

        /* The viewer, over the map, for the pictures of the list */

        function visibility() {
            return {library: SHELL.library() || "", hidden: adminOn && search.hidden,
                    sensitive: search.sensitive || undefined, junk: search.junk};
        }

        // (A new search: without this view, so its map fits it.)
        function chipUrl(group, value) {
            var target = searchLinkState();
            target.q = group === "q" ? value : "";
            target.filters = {};
            target.view = "";
            if (group !== "q") {
                target.filters[group] = [value];
            }
            return SITE.searchUrl(target);
        }

        function libraryName(id) {
            if (indexLibraries.length < 2) {
                return "";
            }
            var name = "";
            indexLibraries.forEach(function (library) {
                if (library.id === id) {
                    name = library.name;
                }
            });
            return name;
        }

        function hashWithId(id) {
            return window.location.pathname + window.location.search +
                (id === null ? "" : "#id=" + encodeURIComponent(id));
        }

        function openViewer(index) {
            if (!selection) {
                return;
            }
            var ids = selection.ids;
            var opened = VIEWER.open({
                ids: ids,
                index: index,
                get: function (id) {
                    return indexById[id] || null;
                },
                query: search.q,
                libraryName: libraryName,
                similar: searchIndex ? function (id) {
                    return SearchCore.similar(searchIndex, id, 8, visibility());
                } : null,
                chipUrl: chipUrl,
                detailUrl: function (id) {
                    return SITE.detailUrl(id, searchLinkState());
                },
                onShow: function (id) {
                    if (viewerPushed) {
                        window.history.replaceState({mapViewer: true}, "", hashWithId(id));
                    }
                },
                onClose: function (id) {
                    if (viewerPushed) {
                        viewerPushed = false;
                        window.history.back();
                    }
                    var item = panelList.querySelector('a[data-index="' + ids.indexOf(id) + '"]');
                    if (item) {
                        item.focus({preventScroll: false});
                    }
                }
            });
            if (opened) {
                // Back closes the viewer (and not the map).
                window.history.pushState({mapViewer: true}, "", hashWithId(ids[index]));
                viewerPushed = true;
            }
        }

        window.addEventListener("popstate", function () {
            if (VIEWER.isOpen() && !/(?:^#|&)id=/.test(window.location.hash || "")) {
                viewerPushed = false;
                VIEWER.close();
            }
        });

        /* Mouse */

        var drag = null;

        function rectPoint(event) {
            var rect = canvas.getBoundingClientRect();
            return {x: event.clientX - rect.left, y: event.clientY - rect.top};
        }

        function clickAt(x, y, slack) {
            var hit = hitPin(pins, x, y, slack);
            if (hit) {
                openPanel(hit.group, false);
            } else {
                closePanel(false);
            }
        }

        // (preventDefault keeps a drag from selecting text, and also stops
        // the click from moving the focus: the map takes it itself, so its
        // keys work after a click even when the list had the focus.)
        function takeFocus() {
            if (document.activeElement !== canvas) {
                canvas.focus({preventScroll: true});
            }
            setUsedBy("pointer");
        }

        canvas.addEventListener("mousedown", function (event) {
            if (event.button !== 0) {
                return;
            }
            event.preventDefault();
            takeFocus();
            var p = rectPoint(event);
            drag = {downX: p.x, downY: p.y, lastX: p.x, lastY: p.y, moved: false};
        });

        window.addEventListener("mousemove", function (event) {
            var p = rectPoint(event);
            if (!drag) {
                if (event.target === canvas) {
                    canvas.classList.toggle("over-pin", Boolean(hitPin(pins, p.x, p.y, MOUSE_SLACK)));
                }
                return;
            }
            if (Math.abs(p.x - drag.downX) + Math.abs(p.y - drag.downY) > 3) {
                drag.moved = true;
            }
            if (drag.moved) {
                panBy(p.x - drag.lastX, p.y - drag.lastY);
            }
            drag.lastX = p.x;
            drag.lastY = p.y;
        });

        window.addEventListener("mouseup", function (event) {
            if (!drag) {
                return;
            }
            if (!drag.moved) {
                var p = rectPoint(event);
                clickAt(p.x, p.y, MOUSE_SLACK);
            }
            drag = null;
        });

        canvas.addEventListener("wheel", function (event) {
            event.preventDefault();
            var move = wheel.feed(event);
            if (move.panX) {
                panBy(move.panX, 0);
            }
            if (move.steps) {
                var p = rectPoint(event);
                zoomAt(p.x, p.y, view.zoom + move.steps);
            }
        }, {passive: false});

        canvas.addEventListener("dblclick", function (event) {
            event.preventDefault();
            var p = rectPoint(event);
            zoomAt(p.x, p.y, view.zoom + (event.shiftKey ? -1 : 1));
        });

        /* Touch */

        var touchState = null;
        var lastTap = null;

        function touchPoint(touch) {
            var rect = canvas.getBoundingClientRect();
            return {x: touch.clientX - rect.left, y: touch.clientY - rect.top};
        }

        function touchList(event) {
            var out = [];
            for (var k = 0; k < event.touches.length; k++) {
                out.push(touchPoint(event.touches[k]));
            }
            return out;
        }

        function spread(a, b) {
            return Math.sqrt((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y));
        }

        function middle(a, b) {
            return {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
        }

        canvas.addEventListener("touchstart", function (event) {
            event.preventDefault();
            takeFocus();
            var touches = touchList(event);
            if (touches.length === 1) {
                touchState = {kind: "pan", lastX: touches[0].x, lastY: touches[0].y,
                              downX: touches[0].x, downY: touches[0].y, moved: false};
            } else if (touches.length >= 2) {
                touchState = {kind: "pinch", startSpread: spread(touches[0], touches[1]),
                              startZoom: view.zoom, lastMid: middle(touches[0], touches[1]),
                              moved: true};
            }
        }, {passive: false});

        canvas.addEventListener("touchmove", function (event) {
            event.preventDefault();
            if (!touchState) {
                return;
            }
            var touches = touchList(event);
            if (touchState.kind === "pan" && touches.length === 1) {
                var p = touches[0];
                if (Math.abs(p.x - touchState.downX) + Math.abs(p.y - touchState.downY) > 6) {
                    touchState.moved = true;
                }
                if (touchState.moved) {
                    panBy(p.x - touchState.lastX, p.y - touchState.lastY);
                }
                touchState.lastX = p.x;
                touchState.lastY = p.y;
            } else if (touchState.kind === "pinch" && touches.length >= 2) {
                var mid = middle(touches[0], touches[1]);
                panBy(mid.x - touchState.lastMid.x, mid.y - touchState.lastMid.y);
                touchState.lastMid = mid;
                var ratio = spread(touches[0], touches[1]) / Math.max(1, touchState.startSpread);
                zoomAt(mid.x, mid.y, touchState.startZoom + Math.log(ratio) / Math.LN2);
            }
        }, {passive: false});

        canvas.addEventListener("touchend", function (event) {
            event.preventDefault();
            if (touchState && touchState.kind === "pan" && !touchState.moved) {
                var now = Date.now();
                // A second tap within 300 ms near the first zooms in.
                if (lastTap && now - lastTap.t < 300 && Math.abs(lastTap.x - touchState.downX) < 30 &&
                        Math.abs(lastTap.y - touchState.downY) < 30) {
                    zoomAt(touchState.downX, touchState.downY, view.zoom + 1);
                    lastTap = null;
                } else {
                    lastTap = {t: now, x: touchState.downX, y: touchState.downY};
                    clickAt(touchState.downX, touchState.downY, TOUCH_SLACK);
                }
            }
            if (event.touches.length === 0) {
                touchState = null;
            } else if (event.touches.length === 1) {
                var t = touchPoint(event.touches[0]);
                touchState = {kind: "pan", lastX: t.x, lastY: t.y, downX: t.x, downY: t.y,
                              moved: true};
            }
        }, {passive: false});

        /* Keyboard: + and - zoom, arrows move, Enter lists the pictures of the
           pin nearest the centre (on the map), Esc closes the list. */

        // (Of the pins whose middle is on the screen.)
        function nearestPin() {
            var best = null;
            var bestDistance = Infinity;
            pins.forEach(function (pin) {
                if (pin.sx < 0 || pin.sx > view.w || pin.sy < 0 || pin.sy > view.h) {
                    return;
                }
                var dx = pin.sx - view.w / 2;
                var dy = pin.sy - view.h / 2;
                var distance = dx * dx + dy * dy;
                if (distance < bestDistance) {
                    best = pin;
                    bestDistance = distance;
                }
            });
            return best;
        }

        document.addEventListener("keydown", function (event) {
            if (VIEWER.isOpen() || event.ctrlKey || event.metaKey || event.altKey) {
                return;
            }
            var target = event.target;
            var tag = target && target.tagName;
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
                    (target && target.isContentEditable)) {
                return;
            }
            var inPanel = panel.contains(target);
            var stepX = Math.max(40, Math.round(view.w / (event.shiftKey ? 2 : 6)));
            var stepY = Math.max(40, Math.round(view.h / (event.shiftKey ? 2 : 6)));
            var key = event.key;
            if (key === "Escape") {
                if (panel.hidden) {
                    return;
                }
                closePanel(true);
            } else if (inPanel) {
                return;
            } else if (key === "+" || key === "=") {
                zoomAt(view.w / 2, view.h / 2, view.zoom + 1);
            } else if (key === "-" || key === "_") {
                zoomAt(view.w / 2, view.h / 2, view.zoom - 1);
            } else if (key === "ArrowLeft") {
                panBy(stepX, 0);
            } else if (key === "ArrowRight") {
                panBy(-stepX, 0);
            } else if (key === "ArrowUp") {
                panBy(0, stepY);
            } else if (key === "ArrowDown") {
                panBy(0, -stepY);
            } else if ((key === "Enter" || key === " ") && target === canvas) {
                var pin = nearestPin();
                if (pin) {
                    openPanel(pin.group, true);
                }
            } else {
                return;
            }
            event.preventDefault();
            if (target === canvas) {
                setUsedBy("key");
            }
        });

        canvas.addEventListener("focus", scheduleDraw);
        canvas.addEventListener("blur", function () {
            setUsedBy("");
            scheduleDraw();
        });

        /* Buttons */

        document.getElementById("zoom-in").addEventListener("click", function () {
            zoomAt(view.w / 2, view.h / 2, view.zoom + 1);
        });
        document.getElementById("zoom-out").addEventListener("click", function () {
            zoomAt(view.w / 2, view.h / 2, view.zoom - 1);
        });

        /* Following the search */

        // "beach - Type: Photos - Year: 2019": what the search asks for, in words.
        function searchSummary() {
            var parts = [];
            if (search.q) {
                parts.push("\u201c" + search.q + "\u201d");
            }
            var labels = {};
            SearchCore.FACETS.forEach(function (facet) {
                labels[facet.key] = facet.label;
            });
            Object.keys(search.filters).forEach(function (group) {
                search.filters[group].forEach(function (value) {
                    var label = group === "library" ? value : SearchCore.facetLabel(group, value);
                    indexLibraries.forEach(function (library) {
                        if (group === "library" && library.id === value) {
                            label = library.name;
                        }
                    });
                    parts.push((labels[group] || group) + ": " + label);
                });
            });
            return parts.join(" \u00b7 ");
        }

        function fit() {
            var spot = fitView(locations, view.w, view.h, TILE, MIN_ZOOM, TILE_ZOOM, 48);
            if (spot) {
                setView(spot.lat, spot.lon, spot.zoom);
            } else {
                worldView();
            }
        }

        // Some pin has its middle on the screen.
        function pinsInSight() {
            var at = corner();
            return screenPins(groupsNow(), at.left, at.top, view.w, view.h, at.w)
                .some(function (pin) {
                    return pin.sx >= 0 && pin.sx <= view.w && pin.sy >= 0 && pin.sy <= view.h;
                });
        }

        function applySearch(refit) {
            var library = SHELL.library();
            var found = SearchCore.search(searchIndex, search.q, {
                library: library || "", libraries: indexLibraries, filters: search.filters,
                hidden: adminOn && search.hidden, sensitive: search.sensitive || undefined,
                junk: search.junk, group: false, sort: search.sort || undefined
            });
            rank = {};
            found.items.forEach(function (id, index) {
                rank[id] = index;
            });
            matchCount = found.total;
            locations = (allLocations || []).filter(function (point) {
                return rank[point.id] !== undefined && validPoint(point);
            });
            locationsVersion += 1;
            closePanel(false);
            var summary = searchSummary();
            document.getElementById("map-search").textContent = summary ? "Showing: " + summary : "";
            // A view from the address that shows none of the pictures (a
            // link made for another search) is no use: fit them instead.
            if (refit || !placed || (viewFromAddress && locations.length && !pinsInSight())) {
                fit();
            }
            viewFromAddress = false;
            if (startPin) {
                var again = groupAt(groupsNow(), startPin.lat, startPin.lon, width());
                startPin = null;
                if (again) {
                    openPanel(again, false);
                } else {
                    SHELL.setState({pin: null});
                }
            }
            updateLinks();
            renderStatus();
            scheduleDraw();
        }

        function load() {
            loadState = "loading";
            renderStatus();
            SITE.loadIndex(function (data) {
                if (!data) {
                    loadState = "failed";
                    renderStatus();
                    return;
                }
                records = data.records;
                indexById = {};
                records.forEach(function (rec) {
                    indexById[rec.i] = rec;
                });
                indexLibraries = data.libraries || [];
                searchIndex = SearchCore.prepare(records);
                SITE.loadLocations(function (loaded) {
                    if (!loaded) {
                        loadState = "failed";
                        renderStatus();
                        return;
                    }
                    allLocations = loaded;
                    loadState = "ready";
                    applySearch(false);
                });
            });
        }

        function init() {
            resize();
            window.addEventListener("resize", resize);
            if (window.ResizeObserver) {
                new window.ResizeObserver(function () {
                    resize();
                }).observe(canvas);
            }
            if (startView) {
                setView(startView.lat, startView.lon, startView.zoom);
            } else {
                worldView();
                placed = false;
            }
            // A viewer left open by a reload: not reopened.
            if (/(?:^#|&)id=/.test(window.location.hash || "")) {
                window.history.replaceState(null, "", hashWithId(null));
            }
            SHELL.onLibraryChange(function () {
                if (loadState === "ready") {
                    applySearch(true);
                }
            });
            updateLinks();
            load();
        }

        init();
    }

    if (hasDocument && document.getElementById("map-canvas")) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", startPage);
        } else {
            startPage();
        }
    }

    return {
        CELL: CELL,
        GAP: GAP,
        MAX_RADIUS: MAX_RADIUS,
        worldWidth: worldWidth,
        wrapX: wrapX,
        normalizeLon: normalizeLon,
        project: project,
        unproject: unproject,
        nearestCopy: nearestCopy,
        copies: copies,
        pinRadius: pinRadius,
        countLabel: countLabel,
        cluster: cluster,
        screenPins: screenPins,
        hitPin: hitPin,
        WheelZoom: WheelZoom,
        fitView: fitView,
        formatView: formatView,
        parseView: parseView,
        formatPoint: formatPoint,
        parsePoint: parsePoint,
        groupAt: groupAt
    };
})();

if (typeof module === "object" && module && module.exports) {
    module.exports = MAP;
}
