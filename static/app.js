/* Search page: instant client-side filtering over a thumbnail grid. */
(function () {
    "use strict";

    var records = [];

    // Hamming distance threshold for stacking near-duplicates. The dhash is
    // 64 bits; same image is 0, light edits a few, unrelated images 20+.
    var CLUSTER_THRESHOLD = 8;

    function el(id) {
        return document.getElementById(id);
    }

    function hamming(a, b) {
        if (!a || !b || a.length !== 16 || b.length !== 16) return 64;
        var d = 0;
        for (var i = 0; i < 16; i += 8) {
            var x = parseInt(a.substr(i, 8), 16) ^ parseInt(b.substr(i, 8), 16);
            x = x - ((x >>> 1) & 0x55555555);
            x = (x & 0x33333333) + ((x >>> 2) & 0x33333333);
            d += (((x + (x >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24;
        }
        return d;
    }

    function clusterByDhash(matches) {
        // Sort by dhash hex (same numeric order as the 64-bit value), then
        // group adjacent records whose Hamming distance is below the
        // threshold. Records without a dhash form their own singleton groups.
        var hashed = [];
        var unhashed = [];
        matches.forEach(function (rec) {
            (rec.dhash ? hashed : unhashed).push(rec);
        });
        hashed.sort(function (a, b) {
            if (a.dhash < b.dhash) return -1;
            if (a.dhash > b.dhash) return 1;
            return 0;
        });
        var groups = [];
        var current = null;
        hashed.forEach(function (rec) {
            if (current && hamming(rec.dhash, current[current.length - 1].dhash) <= CLUSTER_THRESHOLD) {
                current.push(rec);
            } else {
                current = [rec];
                groups.push(current);
            }
        });
        unhashed.forEach(function (rec) { groups.push([rec]); });
        return groups;
    }

    function setupLibraries() {
        var select = el("library");
        if (SITE.libraries.length <= 1) {
            select.style.display = "none";
        }
        var all = document.createElement("option");
        all.value = "__all__";
        all.textContent = "All libraries";
        select.appendChild(all);
        SITE.libraries.forEach(function (name) {
            var option = document.createElement("option");
            option.value = name;
            option.textContent = name;
            select.appendChild(option);
        });
    }

    function tile(rec) {
        var link = document.createElement("a");
        link.className = "tile";
        link.href = SITE.detailUrl(rec.id);
        var img = document.createElement("img");
        img.loading = "lazy";
        img.src = SITE.thumbUrl(rec);
        img.alt = rec.name;
        link.appendChild(img);
        return link;
    }

    function buildCollapsed(node, group) {
        node.innerHTML = "";
        var front = document.createElement("div");
        front.className = "stack-front";
        var img = document.createElement("img");
        img.loading = "lazy";
        img.src = SITE.thumbUrl(group[0]);
        img.alt = group[0].name;
        front.appendChild(img);
        node.appendChild(front);
        var badge = document.createElement("span");
        badge.className = "stack-count";
        badge.textContent = "x" + group.length;
        node.appendChild(badge);
    }

    function buildExpanded(node, group) {
        node.innerHTML = "";
        var head = document.createElement("div");
        head.className = "stack-head";
        var label = document.createElement("span");
        label.textContent = group.length + " similar images";
        head.appendChild(label);
        var close = document.createElement("button");
        close.type = "button";
        close.className = "stack-close";
        close.textContent = "Collapse";
        close.addEventListener("click", function (ev) {
            ev.stopPropagation();
            node.classList.remove("expanded");
            buildCollapsed(node, group);
        });
        head.appendChild(close);
        node.appendChild(head);
        var inner = document.createElement("div");
        inner.className = "stack-grid";
        group.forEach(function (rec) {
            inner.appendChild(tile(rec));
        });
        node.appendChild(inner);
    }

    function stack(group) {
        var node = document.createElement("div");
        node.className = "stack";
        buildCollapsed(node, group);
        node.addEventListener("click", function () {
            if (node.classList.contains("expanded")) return;
            node.classList.add("expanded");
            buildExpanded(node, group);
        });
        return node;
    }

    function message(text) {
        var grid = el("grid");
        grid.innerHTML = "";
        var box = document.createElement("div");
        box.className = "message";
        box.textContent = text;
        grid.appendChild(box);
    }

    function update() {
        var query = el("query").value.trim().toLowerCase();
        var library = el("library").value || "__all__";
        var terms = query.split(/\s+/).filter(Boolean);
        var matches = records.filter(function (rec) {
            if (library !== "__all__" && rec.library !== library) {
                return false;
            }
            var hay = rec.text || "";
            return terms.every(function (term) {
                return hay.indexOf(term) >= 0;
            });
        });
        if (!matches.length) {
            message(records.length ? "No images match." : "No images yet.");
            return;
        }
        var grid = el("grid");
        grid.innerHTML = "";
        if (el("cluster").checked) {
            var groups = clusterByDhash(matches);
            var shown = 0;
            for (var i = 0; i < groups.length && shown < 500; i++) {
                var group = groups[i];
                grid.appendChild(group.length > 1 ? stack(group) : tile(group[0]));
                shown += group.length;
            }
        } else {
            matches.slice(0, 500).forEach(function (rec) {
                grid.appendChild(tile(rec));
            });
        }
    }

    function init() {
        setupLibraries();
        el("map-link").href = SITE.mapUrl();
        el("query").addEventListener("input", update);
        el("library").addEventListener("change", update);
        el("cluster").addEventListener("change", update);
        message("Loading...");
        SITE.loadIndex(function (loaded) {
            if (!loaded) {
                message("Could not load the image index.");
                return;
            }
            records = loaded;
            update();
        });
    }

    document.addEventListener("DOMContentLoaded", init);
})();
