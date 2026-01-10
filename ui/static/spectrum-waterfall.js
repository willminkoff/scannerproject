/**
 * Spectrum Waterfall Visualization
 * Proper waterfall rendering using ImageData for efficient scrolling
 * Based on RTL-SDR and radio receiver patterns
 */

class SpectrumWaterfall {
  constructor() {
    this.canvas = document.getElementById('spectrum-waterfall');
    this.ctx = this.canvas.getContext('2d', { willReadFrequently: true });
    this.header = document.getElementById('spectrum-header');
    this.content = document.getElementById('spectrum-content');
    this.toggle = document.getElementById('spectrum-toggle');
    this.label = document.getElementById('spectrum-label');
    
    this.isExpanded = false;
    this.currentTarget = 'airband';
    this.spectrumData = null;
    this.lastUpdate = 0;
    this.updateInterval = null;
    
    // ImageData for efficient scrolling waterfall
    this.imageData = null;
    this.pixelArray = null;
    this.rowIndex = 0; // Current row in the waterfall
    
    this.colorMap = this.createColorMap();
    
    this.setupEventListeners();
    this.loadPersistentState();
    this.startUpdates();
  }
  
  createColorMap() {
    // Create proper color table: dark -> green -> yellow -> orange -> red
    // Index 0-255 maps to intensity
    const colorTable = new Uint8ClampedArray(256 * 4);
    
    for (let i = 0; i < 256; i++) {
      const ratio = i / 255;
      let r, g, b;
      
      if (ratio < 0.15) {
        // Very quiet: almost black
        r = Math.round(10 + ratio * 20);
        g = Math.round(10 + ratio * 20);
        b = Math.round(20 + ratio * 10);
      } else if (ratio < 0.35) {
        // Quiet: dark blue-green
        const t = (ratio - 0.15) / 0.2;
        r = Math.round(30 + t * 20);
        g = Math.round(30 + t * 50);
        b = Math.round(30 + t * 10);
      } else if (ratio < 0.55) {
        // Light: green
        const t = (ratio - 0.35) / 0.2;
        r = Math.round(50 + t * 100);
        g = Math.round(80 + t * 150);
        b = Math.round(40 + t * 20);
      } else if (ratio < 0.75) {
        // Medium: yellow-green to yellow
        const t = (ratio - 0.55) / 0.2;
        r = Math.round(150 + t * 100);
        g = Math.round(230 + t * 20);
        b = Math.round(60 - t * 50);
      } else {
        // Bright: orange to red
        const t = (ratio - 0.75) / 0.25;
        r = Math.round(250);
        g = Math.round(250 - t * 150);
        b = Math.round(10);
      }
      
      const idx = i * 4;
      colorTable[idx + 0] = r;      // R
      colorTable[idx + 1] = g;      // G
      colorTable[idx + 2] = b;      // B
      colorTable[idx + 3] = 255;    // A
    }
    
    return colorTable;
  }
  
  setupEventListeners() {
    this.header.addEventListener('click', () => this.toggleExpanded());
    window.addEventListener('resize', () => {
      if (this.isExpanded) {
        this.resizeCanvas();
      }
    });
  }
  
  toggleExpanded() {
    this.isExpanded = !this.isExpanded;
    this.updateUI();
    localStorage.setItem('spectrum-expanded', this.isExpanded ? '1' : '0');
  }
  
  loadPersistentState() {
    this.isExpanded = localStorage.getItem('spectrum-expanded') === '1';
    this.updateUI();
  }
  
  updateUI() {
    if (this.isExpanded) {
      this.content.classList.add('expanded');
      this.header.classList.add('expanded');
      this.toggle.textContent = '▼';
      setTimeout(() => {
        this.resizeCanvas();
        this.initImageData();
        this.redraw();
        // Trigger immediate update when expanding
        this.lastUpdate = 0;
        this.updateSpectrum();
      }, 50);
    } else {
      this.content.classList.remove('expanded');
      this.header.classList.remove('expanded');
      this.toggle.textContent = '▲';
    }
  }
  
  resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const displayWidth = this.content.offsetWidth || 400;
    const displayHeight = this.content.offsetHeight || 250;
    
    if (displayWidth <= 0 || displayHeight <= 0) {
      this.canvas.width = 400 * dpr;
      this.canvas.height = 250 * dpr;
    } else {
      this.canvas.width = displayWidth * dpr;
      this.canvas.height = displayHeight * dpr;
    }
    
    this.ctx.scale(dpr, dpr);
  }
  
  initImageData() {
    const width = this.canvas.width;
    const height = this.canvas.height;
    console.debug(`[Waterfall] initImageData: canvas is ${width}x${height}, devicePixelRatio=${window.devicePixelRatio}`);
    
    this.imageData = this.ctx.createImageData(width, height);
    this.pixelArray = this.imageData.data;
    this.rowIndex = 0;
    
    // Initialize with background color
    for (let i = 0; i < this.pixelArray.length; i += 4) {
      this.pixelArray[i + 0] = 5;      // R
      this.pixelArray[i + 1] = 10;     // G
      this.pixelArray[i + 2] = 21;     // B
      this.pixelArray[i + 3] = 255;    // A
    }
    
    // TEST: Draw a test gradient pattern
    console.debug('[Waterfall] Drawing test pattern...');
    for (let x = 0; x < Math.min(width, 100); x++) {
      for (let y = 0; y < 10; y++) {
        const pixelIdx = (y * width + x) * 4;
        this.pixelArray[pixelIdx + 0] = 0;
        this.pixelArray[pixelIdx + 1] = 255;
        this.pixelArray[pixelIdx + 2] = 0;
        this.pixelArray[pixelIdx + 3] = 255;
      }
    }
    
    this.ctx.putImageData(this.imageData, 0, 0);
    console.debug('[Waterfall] putImageData called with test pattern');
  }
  
  async fetchSpectrumData() {
    try {
      const response = await fetch(`/api/spectrum-data?target=${this.currentTarget}&minutes=60`, { cache: 'no-store' });
      const data = await response.json();
      return data.spectrum;
    } catch (e) {
      console.warn('Failed to fetch spectrum data:', e);
      return null;
    }
  }
  
  startUpdates() {
    this.updateInterval = setInterval(() => {
      const now = Date.now();
      if (now - this.lastUpdate > 2000) { // Update every 2 seconds
        this.updateSpectrum();
        this.lastUpdate = now;
      }
    }, 500);
  }
  
  async updateSpectrum() {
    if (!this.isExpanded) {
      return;
    }
    
    if (!this.imageData) {
      console.debug('[Waterfall] No imageData yet, initializing...');
      this.initImageData();
    }
    
    const newData = await this.fetchSpectrumData();
    if (!newData || !newData.data || newData.data.length === 0) {
      console.warn('[Waterfall] No spectrum data received');
      return;
    }
    
    console.debug('[Waterfall] Got spectrum data - rows:', newData.data.length, 'bins:', newData.bins.length);
    this.spectrumData = newData;
    
    // Use the latest spectrum row
    if (newData.data.length > 0) {
      const latestRow = newData.data[newData.data.length - 1];
      console.debug('[Waterfall] Latest powers (first 5):', latestRow.powers.slice(0, 5));
      this.scrollWaterfall(latestRow.powers);
    }
    
    this.redraw();
  }
  
  scrollWaterfall(powers) {
    if (!this.imageData || !this.spectrumData) {
      console.warn('[Waterfall] Missing imageData or spectrumData');
      return;
    }
    
    const width = this.imageData.width;
    const height = this.imageData.height;
    const numBins = this.spectrumData.bins.length;
    
    console.debug(`[Waterfall] Scrolling: ${width}x${height}, ${numBins} bins`);
    
    // Scroll existing data down by one row
    for (let i = 0; i < (height - 1) * width * 4; i++) {
      this.pixelArray[i] = this.pixelArray[i + width * 4];
    }
    
    // Draw new data at top
    const topRowStart = (height - 1) * width * 4;
    const pixelWidth = width / numBins;
    
    let colored = 0;
    for (let binIdx = 0; binIdx < numBins; binIdx++) {
      const power = powers[binIdx] || 0;
      // Scale power (0-10) to intensity (0-255)
      const intensity = Math.min(255, Math.max(0, Math.round((power / 10) * 255)));
      
      // Get color from color map
      const colorIdx = intensity * 4;
      const r = this.colorMap[colorIdx + 0];
      const g = this.colorMap[colorIdx + 1];
      const b = this.colorMap[colorIdx + 2];
      
      if (intensity > 0) colored++;
      
      // Fill pixels for this bin
      const pixelStartX = Math.floor(binIdx * pixelWidth);
      const pixelEndX = Math.floor((binIdx + 1) * pixelWidth);
      
      for (let x = pixelStartX; x < pixelEndX && x < width; x++) {
        const pixelIdx = topRowStart + x * 4;
        this.pixelArray[pixelIdx + 0] = r;
        this.pixelArray[pixelIdx + 1] = g;
        this.pixelArray[pixelIdx + 2] = b;
        this.pixelArray[pixelIdx + 3] = 255;
      }
    }
    console.debug(`[Waterfall] Drew ${colored} colored bins`);
  }
  
  redraw() {
    if (!this.ctx || !this.imageData) return;
    
    // Draw the waterfall image
    this.ctx.putImageData(this.imageData, 0, 0);
    
    // Draw frequency labels and grid
    if (this.spectrumData) {
      this.drawGridOverlay();
    }
  }
  
  drawGridOverlay() {
    const width = this.canvas.width / (window.devicePixelRatio || 1);
    const height = this.canvas.height / (window.devicePixelRatio || 1);
    const numBins = this.spectrumData.bins.length;
    
    const freqMin = this.spectrumData.range.min;
    const freqMax = this.spectrumData.range.max;
    
    // Draw frequency labels at bottom
    this.ctx.font = '10px monospace';
    this.ctx.fillStyle = '#9aa3c7';
    this.ctx.textAlign = 'center';
    
    const step = Math.max(1, Math.round((freqMax - freqMin) / 4));
    for (let f = Math.ceil(freqMin / step) * step; f <= freqMax; f += step) {
      const ratio = (f - freqMin) / (freqMax - freqMin);
      const x = ratio * width;
      
      // Draw subtle grid line
      this.ctx.strokeStyle = 'rgba(255,255,255,0.08)';
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      this.ctx.moveTo(x, 0);
      this.ctx.lineTo(x, height - 14);
      this.ctx.stroke();
      
      // Draw frequency label
      this.ctx.fillText(f.toFixed(0), x, height - 2);
    }
  }
  
  setTarget(target) {
    if (target !== this.currentTarget) {
      this.currentTarget = target;
      this.initImageData();
      if (this.isExpanded) {
        this.updateSpectrum();
      }
    }
  }
  
  destroy() {
    if (this.updateInterval) {
      clearInterval(this.updateInterval);
    }
  }
}

// Create global instance
let spectrumWaterfall = null;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    try {
      spectrumWaterfall = new SpectrumWaterfall();
    } catch (e) {
      console.error('Failed to initialize SpectrumWaterfall:', e);
    }
  });
} else {
  // DOM already loaded
  try {
    spectrumWaterfall = new SpectrumWaterfall();
  } catch (e) {
    console.error('Failed to initialize SpectrumWaterfall:', e);
  }
}
