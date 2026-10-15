const demSource = new mlcontour.DemSource({
    url: window.location.origin +
        "/maps/data/terrain/{z}/{x}/{y}.webp",

    encoding: "terrarium",
    maxzoom: 12,
    worker: true
});

demSource.setupMaplibre(maplibregl);


const AUTO_THRESHOLDS = {
    9:  [500, 2000],
    10: [250, 1000],
    11: [200, 1000],
    12: [100, 500],
    13: [100, 500],
    14: [50, 200],
    15: [20, 100]
};


let topoEnabled = false;
let contourMode = "auto";

let beforeLabelId = null;


function contourThresholds() {

    switch (contourMode) {

        case "50":
            return {
                0: [50, 200]
            };

        case "20":
            return {
                0: [20, 100]
            };

        case "10":
            return {
                0: [10, 50]
            };

        default:
            return AUTO_THRESHOLDS;
    }
}


function addContourSourceAndLayers() {

    map.addSource("terrain-contours", {
        type: "vector",

        tiles: [
            demSource.contourProtocolUrl({
                multiplier: 3.28084,

                overzoom: 1,

                thresholds: contourThresholds(),

                elevationKey: "ele",
                levelKey: "level",
                contourLayer: "contours"
            })
        ],

        maxzoom: 15
    });


    map.addLayer(
        {
            id: "topo-contours",

            type: "line",

            source: "terrain-contours",

            "source-layer": "contours",

            minzoom: 0,

            layout: {
                visibility:
                    topoEnabled ? "visible" : "none"
            },

            paint: {
                "line-color": "#8a6842",

                "line-opacity": 0.75,

                "line-width": [
                    "match",
                    ["get", "level"],
                    1, 1.2,
                    0.55
                ]
            }
        },

        beforeLabelId
    );


    map.addLayer(
        {
            id: "topo-contour-labels",

            type: "symbol",

            source: "terrain-contours",

            "source-layer": "contours",

            minzoom: 9,

            filter: [
                ">",
                ["get", "level"],
                0
            ],

            layout: {
                visibility:
                    topoEnabled ? "visible" : "none",

                "symbol-placement": "line",

                "text-size": 10,

                "text-font": [
                    "Noto Sans Regular"
                ],

                "text-field": [
                    "concat",
                    [
                        "number-format",
                        ["get", "ele"],
                        {
                            "max-fraction-digits": 0
                        }
                    ],
                    " ft"
                ]
            },

            paint: {
                "text-color": "#704d2d",
                "text-halo-color": "#ffffff",
                "text-halo-width": 1.5
            }
        },

        beforeLabelId
    );
}


function rebuildContours() {

    if (map.getLayer("topo-contour-labels")) {
        map.removeLayer("topo-contour-labels");
    }

    if (map.getLayer("topo-contours")) {
        map.removeLayer("topo-contours");
    }

    if (map.getSource("terrain-contours")) {
        map.removeSource("terrain-contours");
    }

    addContourSourceAndLayers();
}


function setTopoVisibility() {

    const visibility =
        topoEnabled ? "visible" : "none";

    [
        "topo-hillshade",
        "topo-contours",
        "topo-contour-labels"
    ].forEach(id => {

        if (map.getLayer(id)) {
            map.setLayoutProperty(
                id,
                "visibility",
                visibility
            );
        }
    });
}


/*
 * Extra early labels.
 *
 * These only display features whose Protomaps min_zoom
 * says they would normally appear within the next one
 * or two zoom levels.
 */

function createMapControls() {

    const style =
        document.createElement("style");


    style.textContent = `
        .library-map-control {
            position: relative;
            display: flex;
            gap: 6px;
            overflow: visible !important;
            font-family: sans-serif;
        }

        .library-map-control > button {
            background: #ffffff;
            border: 0;
            border-radius: 4px;
            box-shadow: 0 0 0 2px rgba(0,0,0,0.10);
        }

        .library-map-control > button:hover {
            background: #f2f2f2;
        }

        .library-map-control button {
            height: 36px !important;
            cursor: pointer;
        }

        .library-topo-button {
            width: 52px !important;
            font-size: 12px !important;
        }

        .library-options-button {
            width: 36px !important;
            padding: 0 !important;
            display: flex !important;
            align-items: center;
            justify-content: center;
        }

        .library-options-button img {
            display: block;
            width: 18px;
            height: 18px;
            margin: auto;
        }

        .map-options-panel {
            position: absolute;

            top: 42px;
            left: 0;

            width: 230px;

            background:
                rgba(255,255,255,0.97);

            border-radius: 5px;

            box-shadow:
                0 1px 6px rgba(0,0,0,0.35);

            padding: 12px;

            display: none;

            color: #222;

            font-size: 14px;
        }

        .map-options-panel.open {
            display: block;
        }

        .map-options-title {
            font-weight: bold;
            font-size: 15px;
            margin-bottom: 10px;
        }

        .map-options-row {
            margin-bottom: 12px;
        }

        .map-options-row:last-child {
            margin-bottom: 0;
        }

        .map-options-label {
            display: block;
            font-weight: bold;
            margin-bottom: 4px;
        }

        .map-options-select {
            width: 100%;
            padding: 4px;
        }

        .map-options-check {
            display: flex;
            align-items: center;
            gap: 7px;
        }

        .map-options-zoom {
            font-family: monospace;
            font-size: 15px;
        }
    `;


    document.head.appendChild(style);


    class LibraryMapControl {

        onAdd(mapInstance) {

            this.map = mapInstance;


            this.container =
                document.createElement("div");

            this.container.className =
                "maplibregl-ctrl " +
                "library-map-control";


            /*
             * TOPO BUTTON
             */

            this.topoButton =
                document.createElement("button");

            this.topoButton.type = "button";

            this.topoButton.className =
                "library-topo-button";

            this.topoButton.textContent =
                "Topo";

            this.topoButton.title =
                "Toggle topographic overlay";


            this.topoButton.onclick = event => {

                event.stopPropagation();

                topoEnabled =
                    !topoEnabled;

                setTopoVisibility();

                this.topoButton.textContent =
                    topoEnabled
                        ? "Map"
                        : "Topo";
            };


            /*
             * GEAR BUTTON
             */

            this.optionsButton =
                document.createElement("button");

            this.optionsButton.type =
                "button";

            this.optionsButton.className =
                "library-options-button";

            this.optionsButton.title =
                "Map options";


            const gear =
                document.createElement("img");

            gear.src =
                "/maps/assets/icons/gear-fill.svg";

            gear.alt = "";


            this.optionsButton.appendChild(
                gear
            );


            /*
             * OPTIONS PANEL
             */

            this.panel =
                document.createElement("div");

            this.panel.className =
                "map-options-panel";


            this.panel.innerHTML = `
                <div class="map-options-title">
                    Map Options
                </div>


                <div class="map-options-row">

                    <span class="map-options-label">
                        Zoom / LOD
                    </span>

                    <span
                        id="map-options-zoom"
                        class="map-options-zoom">
                    </span>

                </div>


                <div class="map-options-row">

                    <label
                        class="map-options-label"
                        for="map-options-contours">

                        Contour interval

                    </label>

                    <select
                        id="map-options-contours"
                        class="map-options-select">

                        <option value="auto">
                            Auto
                        </option>

                        <option value="50">
                            50 ft
                        </option>

                        <option value="20">
                            20 ft
                        </option>

                        <option value="10">
                            10 ft
                        </option>

                    </select>

                </div>


                <div class="map-options-row">

                    <label class="map-options-check">

                        <input
                            type="checkbox"
                            id="map-options-info"
                            checked>

                        Show coordinates / measurement

                    </label>

                </div>
            `;


            this.container.appendChild(
                this.topoButton
            );

            this.container.appendChild(
                this.optionsButton
            );

            this.container.appendChild(
                this.panel
            );


            const zoomDisplay =
                this.panel.querySelector(
                    "#map-options-zoom"
                );


            const contourSelect =
                this.panel.querySelector(
                    "#map-options-contours"
                );


            const infoCheckbox =
                this.panel.querySelector(
                    "#map-options-info"
                );


            const infoPanel =
                document.getElementById(
                    "info"
                );


            const updateZoom = () => {

                if (
                    this.panel.classList.contains(
                        "open"
                    )
                ) {
                    zoomDisplay.textContent =
                        this.map
                            .getZoom()
                            .toFixed(2);
                }
            };


            this.optionsButton.onclick =
                event => {

                    event.stopPropagation();

                    this.panel.classList.toggle(
                        "open"
                    );

                    updateZoom();
                };


            this.panel.onclick =
                event => {

                    event.stopPropagation();
                };


            contourSelect.onchange =
                () => {

                    contourMode =
                        contourSelect.value;

                    rebuildContours();
                };


            infoCheckbox.onchange =
                () => {

                    if (!infoPanel) {
                        return;
                    }

                    infoPanel.style.display =
                        infoCheckbox.checked
                            ? "block"
                            : "none";
                };


            this.zoomHandler =
                updateZoom;


            this.map.on(
                "zoom",
                this.zoomHandler
            );


            return this.container;
        }


        onRemove() {

            this.map.off(
                "zoom",
                this.zoomHandler
            );


            this.container.remove();

            this.map = undefined;
        }
    }


    map.addControl(
        new LibraryMapControl(),
        "top-left"
    );
}


function initializeTopo() {

    const styleLayers =
        map.getStyle().layers;


    const firstSymbol =
        styleLayers.find(
            layer =>
                layer.type === "symbol"
        );


    beforeLabelId =
        firstSymbol
            ? firstSymbol.id
            : undefined;


    map.addSource("terrain-dem", {

        type: "raster-dem",

        tiles: [
            demSource.sharedDemProtocolUrl
        ],

        encoding: "terrarium",

        tileSize: 512,

        maxzoom: 12
    });


    map.addLayer(
        {
            id: "topo-hillshade",

            type: "hillshade",

            source: "terrain-dem",

            layout: {
                visibility: "none"
            },

            paint: {
                "hillshade-exaggeration":
                    0.35,

                "hillshade-shadow-color":
                    "#554b3f",

                "hillshade-highlight-color":
                    "#ffffff"
            }
        },

        beforeLabelId
    );


    addContourSourceAndLayers();

    createMapControls();
}


if (map.loaded()) {
    initializeTopo();
} else {
    map.on(
        "load",
        initializeTopo
    );
}
