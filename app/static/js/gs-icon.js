/**
 * gs-icon.js — TANZANITESAT ground-station symbol (single source of truth).
 *
 * Every map in the site uses the same ground-station marker: the brand
 * dish glyph — the same artwork as the favicon and the sidebar brand icon —
 * rendered in brand blue #4f7cff.
 *
 *   • Leaflet maps (GIS Map View, Satellite Control): TansatGS.divIcon()
 *   • 3D Earth Orbit View:                            TansatGS.sprite()
 *   • Static HTML legends:                            TansatGS.svg()
 *
 * Keep the path data in sync with app/static/favicon.svg if the brand
 * dish artwork ever changes.
 */
(function () {
  'use strict';

  var GS_COLOR = '#4f7cff';

  /**
   * Inline SVG string of the dish glyph (viewBox 0 0 24 24).
   * @param {number} size  rendered width/height in px
   * @param {string} [color] stroke/fill color, defaults to brand blue
   */
  function svg(size, color) {
    var c = color || GS_COLOR;
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"' +
      ' width="' + size + '" height="' + size + '"' +
      ' fill="none" stroke="' + c + '" stroke-width="2"' +
      ' stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 15a9 9 0 0118 0"/>' +
      '<path d="M12 15v6"/>' +
      '<path d="M8 21h8"/>' +
      '<path d="M12 15L20 6"/>' +
      '<circle cx="12" cy="15" r="1.8" fill="' + c + '" stroke="none"/>' +
      '</svg>';
  }

  /**
   * Leaflet divIcon for the ground-station marker. The glyph is centered
   * on the coordinate (same anchor semantics as the old circle markers).
   * @param {{size?: number, color?: string}} [opts]
   */
  function divIcon(opts) {
    opts = opts || {};
    var size = opts.size || 28;
    var color = opts.color || GS_COLOR;
    return L.divIcon({
      className: 'gs-marker',
      html: '<div style="display:flex;align-items:center;justify-content:center;' +
        'width:' + size + 'px;height:' + size + 'px;' +
        'filter:drop-shadow(0 0 4px rgba(79,124,255,.55));">' +
        svg(size, color) + '</div>',
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
      popupAnchor: [0, -size / 2],
    });
  }

  /**
   * Canvas painter for the 3D sprite. Draws the dish glyph centered on a
   * size×size transparent canvas, with a dark halo pass so it stays legible
   * over bright Earth imagery.
   * @param {HTMLCanvasElement} canvas
   * @param {string} [color]
   */
  function drawOnCanvas(canvas, color) {
    var ctx = canvas.getContext('2d');
    var size = canvas.width;
    ctx.clearRect(0, 0, size, size);
    var s = size / 24;
    ctx.save();
    ctx.scale(s, s);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    // Geometry (24-unit viewBox, matches the SVG):
    //   dish bowl: upper semicircle from (3,15) through (12,6) to (21,15)
    //   mast (12,15)→(12,21) · base (8,21)→(16,21) · beam (12,15)→(20,6)
    //   focus dot: filled circle r1.8 at (12,15)
    function glyph(strokeStyle, lineWidth, fillStyle) {
      ctx.strokeStyle = strokeStyle;
      ctx.lineWidth = lineWidth;
      ctx.fillStyle = fillStyle;
      ctx.beginPath();
      ctx.arc(12, 15, 9, Math.PI, 2 * Math.PI, false);
      ctx.moveTo(12, 15); ctx.lineTo(12, 21);
      ctx.moveTo(8, 21);  ctx.lineTo(16, 21);
      ctx.moveTo(12, 15); ctx.lineTo(20, 6);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(12, 15, 1.8, 0, 2 * Math.PI);
      ctx.fill();
    }

    var c = color || GS_COLOR;
    glyph('rgba(5,5,8,0.85)', 4.4, 'rgba(5,5,8,0.85)'); // dark halo pass
    glyph(c, 2, c);                                      // brand color pass
    ctx.restore();
  }

  /**
   * THREE.Sprite billboard of the dish glyph for the 3D Earth Orbit View.
   * Depth-tested, so the marker hides behind the globe on the far side —
   * same behavior as the old sphere marker.
   * @param {{color?: string, scale?: number}} [opts] scale in scene units
   */
  function sprite(opts) {
    opts = opts || {};
    var px = 128;
    var canvas = document.createElement('canvas');
    canvas.width = px;
    canvas.height = px;
    drawOnCanvas(canvas, opts.color);

    var tex = new THREE.CanvasTexture(canvas);
    if (THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
    tex.needsUpdate = true;

    var sp = new THREE.Sprite(new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      depthTest: true,
    }));
    var scale = opts.scale || 5;
    sp.scale.set(scale, scale, 1);
    return sp;
  }

  window.TansatGS = {
    color: GS_COLOR,
    svg: svg,
    divIcon: divIcon,
    sprite: sprite,
    drawOnCanvas: drawOnCanvas,
  };
})();
