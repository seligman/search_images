/* Search page: instant client-side filtering over a thumbnail grid. */
(function () {
    "use strict";

    var records = [];

    // Lookup table: number of set bits in each 4-bit nibble. Used to total
    // the Hamming distance between two 16-character hex dhash strings.
    var NIBBLE_BITS = [0,1,1,2,1,2,2,3,1,2,2,3,2,3,3,4];

    function el(id) {
        return document.getElementById(id);
    }

    function hammingHex(a, b) {
        var distance = 0;
        for (var i = 0; i < 16; i++) {
            distance += NIBBLE_BITS[parseInt(a.charAt(i), 16)
                ^ parseInt(b.charAt(i), 16)];
        }
        return distance;
    }

    function clusterByDhash(matches) {
        // Greedy nearest-neighbour chain: pick a seed, then repeatedly append
        // the unused record with the smallest Hamming distance to the tail.
        // Records without a dhash drop to the end in their original order.
        var hashed = [];
        var unhashed = [];
        matches.forEach(function (rec) {
            (rec.dhash ? hashed : unhashed).push(rec);
        });
        if (hashed.length < 2) {
            return hashed.concat(unhashed);
        }
        var ordered = [hashed.shift()];
        while (hashed.length) {
            var tail = ordered[ordered.length - 1].dhash;
            var bestIdx = 0;
            var bestDist = Infinity;
            for (var i = 0; i < hashed.length; i++) {
                var d = hammingHex(tail, hashed[i].dhash);
                if (d < bestDist) {
                    bestDist = d;
                    bestIdx = i;
                    if (d === 0) {
                        break;
                    }
                }
            }
            ordered.push(hashed.splice(bestIdx, 1)[0]);
        }
        return ordered.concat(unhashed);
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
        if (el("cluster").checked) {
            matches = clusterByDhash(matches);
        }
        var grid = el("grid");
        grid.innerHTML = "";
        matches.slice(0, 500).forEach(function (rec) {
            grid.appendChild(tile(rec));
        });
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
