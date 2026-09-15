/* Choosing a ride's pickup and destination: typed search, a tap on the map, or
   the traveller's own location.

   The logic lives here, apart from Leaflet and the page, so a browser test can
   drive it with a fake map, a fake geolocation and a fake reverse geocoder.

   Rules:
   - Location is read only when the traveller taps "Use my location": one
     reading, never a watch, never on page load.
   - A chosen point keeps its exact coordinates. The text beside it is what the
     driver reads, and the traveller may edit it without losing the point.
   - When the browser refuses location, or no address can be found for a point,
     the traveller is told plainly and can still tap the map or type.
*/
(function (global) {
  "use strict";

  var NAMES = { pickup: "pickup", dropoff: "destination" };

  function round6(value) {
    return Math.round(Number(value) * 1e6) / 1e6;
  }

  function validPoint(lat, lng) {
    return typeof lat === "number" && typeof lng === "number" &&
      isFinite(lat) && isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180;
  }

  function createRidePicker(deps) {
    var points = { pickup: null, dropoff: null };
    var target = null;           // which point the next map tap sets
    var lookup = { pickup: 0, dropoff: 0 };
    var locating = false;

    function changed() { if (deps.ui.changed) deps.ui.changed(); }

    function setTarget(which) {
      target = which;
      deps.ui.setMode(which);
    }

    function placeLabel(which, label, source) {
      points[which].label = label;
      points[which].source = source;
      deps.ui.setAddress(which, label);
    }

    function setPoint(which, lat, lng, source) {
      lat = round6(lat); lng = round6(lng);
      if (!validPoint(lat, lng)) return false;
      points[which] = { lat: lat, lng: lng, label: "", source: source };
      deps.map.setMarker(which, lat, lng);
      changed();
      return true;
    }

    function fallbackLabel(point) {
      return "Pinned location (" + point.lat.toFixed(5) + ", " + point.lng.toFixed(5) + ")";
    }

    function describe(which, source) {
      var point = points[which];
      var ticket = ++lookup[which];
      deps.ui.setStatus(which, "Finding the address for this " + NAMES[which] + "…", "info");
      return Promise.resolve()
        .then(function () { return deps.reverseGeocode(point.lat, point.lng); })
        .then(function (place) {
          if (ticket !== lookup[which] || points[which] !== point) return;   // moved since
          if (place && place.label) {
            placeLabel(which, place.label, source);
            deps.ui.setStatus(which, capital(NAMES[which]) + " set. Check the address, or tap the map to move it.", "good");
          } else {
            placeLabel(which, fallbackLabel(point), source);
            deps.ui.setStatus(which, "We couldn't find an address for that spot. The pin is saved — add a landmark so your driver can find it.", "warn");
          }
        }, function () {
          if (ticket !== lookup[which] || points[which] !== point) return;
          placeLabel(which, fallbackLabel(point), source);
          deps.ui.setStatus(which, "Address lookup isn't working right now. The pin is saved — add a landmark so your driver can find it.", "warn");
        });
    }

    function capital(text) { return text.charAt(0).toUpperCase() + text.slice(1); }

    return {
      points: function () {
        return {
          pickup: points.pickup && { lat: points.pickup.lat, lng: points.pickup.lng, label: points.pickup.label, source: points.pickup.source },
          dropoff: points.dropoff && { lat: points.dropoff.lat, lng: points.dropoff.lng, label: points.dropoff.label, source: points.dropoff.source }
        };
      },
      target: function () { return target; },

      chooseOnMap: function (which) {
        if (!deps.map.available) {
          deps.ui.setStatus(which, "The map isn't available. Type the " + NAMES[which] + " and pick it from the list.", "warn");
          return;
        }
        setTarget(which);
        deps.map.focus();
        deps.ui.setStatus(which, "Tap the map where the " + NAMES[which] + " is.", "info");
      },

      /* With a point selected ("Choose … on map", or after "Use my location"),
         every tap moves that point. With none selected, taps fill the pickup
         first and then the destination. */
      mapTapped: function (lat, lng) {
        var which = target || (!points.pickup ? "pickup" : (!points.dropoff ? "dropoff" : null));
        if (!which) {
          deps.ui.setStatus("dropoff", "Both points are set. Tap “Choose … on map” next to the one you want to move.", "info");
          return null;
        }
        if (!setPoint(which, lat, lng, "map")) return null;
        describe(which, "map");
        return which;
      },

      useMyLocation: function () {
        if (locating) return Promise.resolve(false);
        var geo = deps.geolocation;
        if (!geo || typeof geo.getCurrentPosition !== "function") {
          deps.ui.setStatus("pickup", "This browser can't share your location. Tap the map or type your pickup.", "warn");
          return Promise.resolve(false);
        }
        locating = true;
        deps.ui.setLocating(true);
        deps.ui.setStatus("pickup", "Asking your browser for your location…", "info");
        var self = this;
        return new Promise(function (resolve) {
          geo.getCurrentPosition(function (position) {
            locating = false;
            deps.ui.setLocating(false);
            var ok = setPoint("pickup", position.coords.latitude, position.coords.longitude, "location");
            if (!ok) {
              deps.ui.setStatus("pickup", "Your browser gave an unusable location. Tap the map or type your pickup.", "warn");
              resolve(false);
              return;
            }
            setTarget("pickup");
            deps.map.focus();
            describe("pickup", "location").then(function () { resolve(true); });
          }, function (error) {
            locating = false;
            deps.ui.setLocating(false);
            var message = error && error.code === 1
              ? "Location permission was turned off, so we can't use your location. Tap the map or type your pickup instead."
              : (error && error.code === 3
                ? "Finding your location took too long. Try again, tap the map, or type your pickup."
                : "Your location isn't available right now. Tap the map or type your pickup.");
            deps.ui.setStatus("pickup", message, "warn");
            setTarget("pickup");
            resolve(false);
          }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
        });
      },

      searchSelected: function (which, place) {
        if (!place || !setPoint(which, Number(place.lat), Number(place.lng), "search")) return false;
        lookup[which]++;          // a pending reverse lookup must not overwrite this
        placeLabel(which, String(place.label || ""), "search");
        deps.ui.setStatus(which, capital(NAMES[which]) + " set.", "good");
        return true;
      },

      labelEdited: function (which, text) {
        if (points[which]) points[which].label = String(text || "");
      },

      clear: function (which) {
        points[which] = null;
        lookup[which]++;
        deps.map.removeMarker(which);
        deps.ui.setAddress(which, "");
        deps.ui.setStatus(which, "", "info");
        if (target === which) setTarget(null);
        changed();
      }
    };
  }

  global.createRidePicker = createRidePicker;
})(typeof window !== "undefined" ? window : this);
