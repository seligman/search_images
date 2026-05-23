/* World-map view: tile pyramid with pin clustering, pan, and pinch zoom.

   Coordinates:
     - lat/lon: WGS84 degrees.
     - world pixels at zoom z: the equirectangular projection sized so the
       world is (TILE_SIZE * 2^(z+1)) wide and (TILE_SIZE * 2^z) tall.
     - screen pixels: position inside the canvas.

   The canvas is the only thing being drawn to. Tiles are loaded lazily and
   cached; cluster pins are computed on every frame from the projected marker
   positions, so they merge or split smoothly as the user zooms.
*/
(function () {
    "use strict";

    var TILE_SIZE = SITE.tileSize();
    var TILES_MAX = SITE.tilesMax();
    var MIN_ZOOM = 0;
    // When no tile pyramid is configured the map still works on the bundled
    // fallback basemap, but we cap zoom so we never request tiles that don't
    // exist. Cluster math keeps working at any zoom.
    var MAX_ZOOM = TILES_MAX === null ? 3 : Math.max(TILES_MAX, MIN_ZOOM);
    var CLUSTER_RADIUS = 28;

    var canvas = document.getElementById("map-canvas");
    var ctx = canvas.getContext("2d");
    var panel = document.getElementById("map-panel");
    var panelList = document.getElementById("map-panel-list");
    var panelTitle = document.getElementById("map-panel-title");
    var statusBar = document.getElementById("map-status");

    var locations = [];
    var indexById = {};
    var tileCache = {};
    var tileQueue = [];
    var lastClusters = [];
    var dpr = window.devicePixelRatio || 1;

    var state = {
        centerLat: 20,
        centerLon: 0,
        zoom: 1,
        screenW: 0,
        screenH: 0
    };

    function clamp(value, lo, hi) {
        return value < lo ? lo : (value > hi ? hi : value);
    }

    function worldSize(zoom) {
        return TILE_SIZE * Math.pow(2, zoom + 1);
    }

    function lonToWorld(lon, zoom) {
        return (lon + 180) / 360 * worldSize(zoom);
    }

    function latToWorld(lat, zoom) {
        return (90 - lat) / 180 * (worldSize(zoom) / 2);
    }

    function worldToLon(wx, zoom) {
        return wx * 360 / worldSize(zoom) - 180;
    }

    function worldToLat(wy, zoom) {
        return 90 - wy * 180 / (worldSize(zoom) / 2);
    }

    function resize() {
        var rect = canvas.getBoundingClientRect();
        state.screenW = rect.width;
        state.screenH = rect.height;
        canvas.width = Math.floor(rect.width * dpr);
        canvas.height = Math.floor(rect.height * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        clampCenter();
        scheduleDraw();
    }

    function clampCenter() {
        var halfH = state.screenH / 2;
        var ws = worldSize(state.zoom);
        var hs = ws / 2;
        // Vertical clamp: keep the visible window inside the world rectangle.
        var topY = halfH;
        var bottomY = hs - halfH;
        if (bottomY < topY) {
            state.centerLat = 0;
        } else {
            var cy = clamp(latToWorld(state.centerLat, state.zoom), topY, bottomY);
            state.centerLat = worldToLat(cy, state.zoom);
        }
        // Longitude wraps; no clamp needed but normalise to [-180, 180).
        state.centerLon = ((state.centerLon + 540) % 360) - 180;
    }

    /* ---------- Tile loading and rendering ----------------------------- */

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

    function drawTiles() {
        var z = state.zoom;
        var ws = worldSize(z);
        var hs = ws / 2;
        var cols = Math.pow(2, z + 1);
        var rows = Math.pow(2, z);
        var cx = lonToWorld(state.centerLon, z);
        var cy = latToWorld(state.centerLat, z);
        var leftWorld = cx - state.screenW / 2;
        var topWorld = cy - state.screenH / 2;
        var minTileX = Math.floor(leftWorld / TILE_SIZE);
        var maxTileX = Math.floor((leftWorld + state.screenW) / TILE_SIZE);
        var minTileY = Math.max(0, Math.floor(topWorld / TILE_SIZE));
        var maxTileY = Math.min(rows - 1,
            Math.floor((topWorld + state.screenH) / TILE_SIZE));
        for (var ty = minTileY; ty <= maxTileY; ty++) {
            for (var tx = minTileX; tx <= maxTileX; tx++) {
                // Longitude wraps: a tile at column tx is the same image as
                // ((tx % cols) + cols) % cols, just drawn at a shifted x.
                var wrappedX = ((tx % cols) + cols) % cols;
                var entry = getTile(z, wrappedX, ty);
                var dx = tx * TILE_SIZE - leftWorld;
                var dy = ty * TILE_SIZE - topWorld;
                if (entry && entry.loaded) {
                    ctx.drawImage(entry.img, dx, dy, TILE_SIZE, TILE_SIZE);
                } else {
                    ctx.fillStyle = "#1a2c3d";
                    ctx.fillRect(dx, dy, TILE_SIZE, TILE_SIZE);
                }
            }
        }
        // Avoid unbounded cache growth in long sessions.
        var keys = Object.keys(tileCache);
        if (keys.length > 400) {
            // Drop the oldest half; cache order is insertion order in modern JS.
            for (var i = 0; i < keys.length - 200; i++) {
                delete tileCache[keys[i]];
            }
        }
    }

    /* ---------- Marker projection + screen clustering ------------------ */

    function projectMarkers() {
        var z = state.zoom;
        var cx = lonToWorld(state.centerLon, z);
        var cy = latToWorld(state.centerLat, z);
        var leftWorld = cx - state.screenW / 2;
        var topWorld = cy - state.screenH / 2;
        var ws = worldSize(z);
        var out = [];
        for (var i = 0; i < locations.length; i++) {
            var loc = locations[i];
            var wx = lonToWorld(loc.lon, z);
            var wy = latToWorld(loc.lat, z);
            // Pick the longitude wrap closest to the viewport so a marker on
            // the date line still shows when the user is panned across.
            while (wx - leftWorld < -ws / 2) {
                wx += ws;
            }
            while (wx - leftWorld > ws + ws / 2) {
                wx -= ws;
            }
            var sx = wx - leftWorld;
            var sy = wy - topWorld;
            if (sx < -CLUSTER_RADIUS || sx > state.screenW + CLUSTER_RADIUS
                    || sy < -CLUSTER_RADIUS || sy > state.screenH + CLUSTER_RADIUS) {
                continue;
            }
            out.push({id: loc.id, sx: sx, sy: sy});
        }
        return out;
    }

    function clusterMarkers(markers) {
        // Bucket by integer cell at half the cluster radius; merge neighbours.
        var step = CLUSTER_RADIUS;
        var buckets = {};
        for (var i = 0; i < markers.length; i++) {
            var m = markers[i];
            var bx = Math.floor(m.sx / step);
            var by = Math.floor(m.sy / step);
            var key = bx + "," + by;
            if (!buckets[key]) {
                buckets[key] = {sx: 0, sy: 0, ids: []};
            }
            buckets[key].sx += m.sx;
            buckets[key].sy += m.sy;
            buckets[key].ids.push(m.id);
        }
        var clusters = [];
        var keys = Object.keys(buckets);
        for (var k = 0; k < keys.length; k++) {
            var b = buckets[keys[k]];
            clusters.push({
                sx: b.sx / b.ids.length,
                sy: b.sy / b.ids.length,
                ids: b.ids
            });
        }
        return clusters;
    }

    function drawClusters(clusters) {
        ctx.save();
        ctx.font = "600 12px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        for (var i = 0; i < clusters.length; i++) {
            var c = clusters[i];
            var n = c.ids.length;
            var r = n === 1 ? 7 : Math.min(22, 9 + Math.log(n) * 4);
            ctx.beginPath();
            ctx.fillStyle = n === 1 ? "rgba(220, 60, 60, 0.92)"
                                    : "rgba(40, 110, 220, 0.92)";
            ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
            ctx.lineWidth = 2;
            ctx.arc(c.sx, c.sy, r, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
            if (n > 1) {
                ctx.fillStyle = "#fff";
                ctx.fillText(String(n), c.sx, c.sy + 0.5);
            }
            c.r = r;
        }
        ctx.restore();
    }

    /* ---------- Frame scheduling --------------------------------------- */

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
        ctx.fillStyle = "#11161c";
        ctx.fillRect(0, 0, state.screenW, state.screenH);
        drawTiles();
        var markers = projectMarkers();
        lastClusters = clusterMarkers(markers);
        drawClusters(lastClusters);
        if (statusBar) {
            statusBar.textContent = locations.length + " geotagged image"
                + (locations.length === 1 ? "" : "s")
                + " | zoom " + state.zoom;
        }
    }

    /* ---------- Interaction: pan, zoom, click -------------------------- */

    function screenToLatLon(sx, sy) {
        var z = state.zoom;
        var cx = lonToWorld(state.centerLon, z);
        var cy = latToWorld(state.centerLat, z);
        var wx = cx - state.screenW / 2 + sx;
        var wy = cy - state.screenH / 2 + sy;
        return {
            lat: worldToLat(wy, z),
            lon: worldToLon(wx, z)
        };
    }

    function zoomAt(sx, sy, newZoom) {
        newZoom = clamp(Math.round(newZoom), MIN_ZOOM, MAX_ZOOM);
        if (newZoom === state.zoom) {
            return;
        }
        // Keep the world point under (sx, sy) fixed on screen.
        var anchor = screenToLatLon(sx, sy);
        state.zoom = newZoom;
        var anchorWx = lonToWorld(anchor.lon, newZoom);
        var anchorWy = latToWorld(anchor.lat, newZoom);
        state.centerLon = worldToLon(anchorWx + state.screenW / 2 - sx, newZoom);
        state.centerLat = worldToLat(anchorWy + state.screenH / 2 - sy, newZoom);
        clampCenter();
        scheduleDraw();
    }

    function panByPixels(dx, dy) {
        var z = state.zoom;
        state.centerLon -= dx * 360 / worldSize(z);
        state.centerLat += dy * 180 / (worldSize(z) / 2);
        clampCenter();
        scheduleDraw();
    }

    var pointers = {};

    function pointerKey(event, kind) {
        if (kind === "touch") {
            return "t" + event.identifier;
        }
        return "m";
    }

    function rectPoint(event) {
        var rect = canvas.getBoundingClientRect();
        return {x: event.clientX - rect.left, y: event.clientY - rect.top};
    }

    function rectTouch(touch) {
        var rect = canvas.getBoundingClientRect();
        return {x: touch.clientX - rect.left, y: touch.clientY - rect.top};
    }

    function hitCluster(sx, sy) {
        // Walk in reverse so the visually top-most pin wins on overlap.
        for (var i = lastClusters.length - 1; i >= 0; i--) {
            var c = lastClusters[i];
            var dx = c.sx - sx;
            var dy = c.sy - sy;
            var r = (c.r || 8) + 4;
            if (dx * dx + dy * dy <= r * r) {
                return c;
            }
        }
        return null;
    }

    function openPanel(cluster) {
        panel.hidden = false;
        panelTitle.textContent = cluster.ids.length + " image"
            + (cluster.ids.length === 1 ? "" : "s");
        panelList.innerHTML = "";
        cluster.ids.forEach(function (id) {
            var rec = indexById[id];
            if (!rec) {
                return;
            }
            var link = document.createElement("a");
            link.href = SITE.detailUrl(id);
            link.className = "map-panel-item";
            var img = document.createElement("img");
            img.loading = "lazy";
            img.src = SITE.thumbUrl(rec);
            img.alt = rec.name;
            var label = document.createElement("span");
            label.textContent = rec.name;
            link.appendChild(img);
            link.appendChild(label);
            panelList.appendChild(link);
        });
    }

    function closePanel() {
        panel.hidden = true;
    }

    /* Mouse */

    canvas.addEventListener("mousedown", function (event) {
        event.preventDefault();
        var p = rectPoint(event);
        pointers.m = {downX: p.x, downY: p.y, lastX: p.x, lastY: p.y,
                      moved: false, kind: "mouse"};
    });

    window.addEventListener("mousemove", function (event) {
        var p = pointers.m;
        if (!p) {
            return;
        }
        var cur = rectPoint(event);
        var dx = cur.x - p.lastX;
        var dy = cur.y - p.lastY;
        if (Math.abs(cur.x - p.downX) + Math.abs(cur.y - p.downY) > 3) {
            p.moved = true;
        }
        if (p.moved) {
            panByPixels(dx, dy);
        }
        p.lastX = cur.x;
        p.lastY = cur.y;
    });

    window.addEventListener("mouseup", function (event) {
        var p = pointers.m;
        if (!p) {
            return;
        }
        if (!p.moved) {
            var pt = rectPoint(event);
            var hit = hitCluster(pt.x, pt.y);
            if (hit) {
                openPanel(hit);
            }
        }
        delete pointers.m;
    });

    canvas.addEventListener("wheel", function (event) {
        event.preventDefault();
        var p = rectPoint(event);
        var step = event.deltaY < 0 ? 1 : -1;
        zoomAt(p.x, p.y, state.zoom + step);
    }, {passive: false});

    canvas.addEventListener("dblclick", function (event) {
        event.preventDefault();
        var p = rectPoint(event);
        zoomAt(p.x, p.y, state.zoom + 1);
    });

    /* Touch */

    var touchState = null;
    var lastTap = null;

    function distance(a, b) {
        var dx = a.x - b.x;
        var dy = a.y - b.y;
        return Math.sqrt(dx * dx + dy * dy);
    }

    function midpoint(a, b) {
        return {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
    }

    canvas.addEventListener("touchstart", function (event) {
        event.preventDefault();
        var touches = [];
        for (var i = 0; i < event.touches.length; i++) {
            touches.push(rectTouch(event.touches[i]));
        }
        if (touches.length === 1) {
            touchState = {
                kind: "pan",
                lastX: touches[0].x, lastY: touches[0].y,
                downX: touches[0].x, downY: touches[0].y,
                moved: false
            };
        } else if (touches.length >= 2) {
            touchState = {
                kind: "pinch",
                startDist: distance(touches[0], touches[1]),
                startZoom: state.zoom,
                lastMid: midpoint(touches[0], touches[1]),
                anchor: midpoint(touches[0], touches[1]),
                moved: true
            };
        }
    }, {passive: false});

    canvas.addEventListener("touchmove", function (event) {
        event.preventDefault();
        if (!touchState) {
            return;
        }
        var touches = [];
        for (var i = 0; i < event.touches.length; i++) {
            touches.push(rectTouch(event.touches[i]));
        }
        if (touchState.kind === "pan" && touches.length === 1) {
            var p = touches[0];
            var dx = p.x - touchState.lastX;
            var dy = p.y - touchState.lastY;
            if (Math.abs(p.x - touchState.downX)
                    + Math.abs(p.y - touchState.downY) > 6) {
                touchState.moved = true;
            }
            if (touchState.moved) {
                panByPixels(dx, dy);
            }
            touchState.lastX = p.x;
            touchState.lastY = p.y;
        } else if (touchState.kind === "pinch" && touches.length >= 2) {
            var d = distance(touches[0], touches[1]);
            var mid = midpoint(touches[0], touches[1]);
            // Move-by-midpoint: this gives a natural drag-while-pinching feel.
            panByPixels(mid.x - touchState.lastMid.x,
                        mid.y - touchState.lastMid.y);
            touchState.lastMid = mid;
            var ratio = d / touchState.startDist;
            var target = touchState.startZoom + Math.log(ratio) / Math.LN2;
            zoomAt(mid.x, mid.y, target);
        }
    }, {passive: false});

    canvas.addEventListener("touchend", function (event) {
        event.preventDefault();
        if (touchState && touchState.kind === "pan" && !touchState.moved) {
            var now = Date.now();
            // Double-tap zoom: second tap within 300ms and ~30px of the first.
            if (lastTap && now - lastTap.t < 300
                    && Math.abs(lastTap.x - touchState.downX) < 30
                    && Math.abs(lastTap.y - touchState.downY) < 30) {
                zoomAt(touchState.downX, touchState.downY, state.zoom + 1);
                lastTap = null;
            } else {
                lastTap = {t: now, x: touchState.downX, y: touchState.downY};
                var hit = hitCluster(touchState.downX, touchState.downY);
                if (hit) {
                    openPanel(hit);
                }
            }
        }
        if (event.touches.length === 0) {
            touchState = null;
        } else if (event.touches.length === 1) {
            var t = rectTouch(event.touches[0]);
            touchState = {kind: "pan", lastX: t.x, lastY: t.y,
                          downX: t.x, downY: t.y, moved: true};
        }
    }, {passive: false});

    /* Buttons */

    document.getElementById("zoom-in").addEventListener("click", function () {
        zoomAt(state.screenW / 2, state.screenH / 2, state.zoom + 1);
    });
    document.getElementById("zoom-out").addEventListener("click", function () {
        zoomAt(state.screenW / 2, state.screenH / 2, state.zoom - 1);
    });
    document.getElementById("map-panel-close").addEventListener(
        "click", closePanel);
    document.getElementById("map-back").addEventListener(
        "click", function (event) {
            event.preventDefault();
            window.location.href = SITE.homeUrl();
        });

    /* ---------- Initial data load -------------------------------------- */

    function init() {
        resize();
        window.addEventListener("resize", resize);
        SITE.loadIndex(function (records) {
            if (records) {
                records.forEach(function (rec) {
                    indexById[rec.id] = rec;
                });
            }
            SITE.loadLocations(function (loaded) {
                locations = loaded || [];
                if (!locations.length && statusBar) {
                    statusBar.textContent =
                        "No geotagged images found in the library.";
                }
                scheduleDraw();
            });
        });
    }

    document.addEventListener("DOMContentLoaded", init);
})();
