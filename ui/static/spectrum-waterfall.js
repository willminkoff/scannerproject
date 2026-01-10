/**
 * Spectrum Waterfall Visualization
 * Renders frequency activity as a scrolling waterfall using Canvas API
 */

class SpectrumWaterfall {
  constructor() {
    this.canvas = document.getElementById('spectrum-waterfall');
    this.ctx = this.canvas.getContext('2d');
    this.header = document.getElementById('spectrum-header');
    this.content = document.getElementById('spectrum-content');
    this.toggle = document.getElementById('spectrum-toggle');
    this.label = document.getElementById('spectrum-label');
    
    this.isExpanded = false;
    this.currentTarget = 'airband';
    this.spectrumData = null;
    this.pixelBuffer = []; // Rolling buffer of spectrum rows
    this.updateInterval = null;
    this.lastUpdate = 0;
    this.gridPixels = 20; // Height of each time row in pixels
    this.colorMap = this.createColorMap();
    
    this.setupEventListeners();
    this.loadPersistentState();
    this.startUpdates();
  }
  
  createColorMap() {
    // Create a color gradient: dark -> green -> yellow -> red
    // Returns a canvas gradient that maps power levels to colors
    const map = {};
    for (let i = 0; i <= 10; i++) {
      const ratio = i / 10;
      let color;
      if (ratio < 0.2) {
        // Dark gray for quiet
        color = this.lerpColor([20, 20, 30], [20, 20, 30], ratio / 0.2);
      } else if (ratio < 0.4) {
        // Dark to green
        color = this.lerpColor([20, 20, 30], [34, 197, 94], (ratio - 0.2) / 0.2);
      } else if (ratio < 0.7) {
        // Green to yellow
        color = this.lerpColor([34, 197, 94], [251, 191, 36], (ratio - 0.4) / 0.3);
      } else {
        // Yellow to red
        color = this.lerpColor([251, 191, 36], [239, 68, 68], (ratio - 0.7) / 0.3);
      }
      map[i] = `rgb(${Math.round(color[0])},${Math.round(color[1])},${Math.round(color[2])})`;
    }
    return map;
  }
  
  lerpColor(color1, color2, t) {
    return [
      color1[0] + (color2[0] - color1[0]) * t,
      color1[1] + (color2[1] - color1[1]) * t,
      color1[2] + (color2[2] - color1[2]) * t,
    ];
  }
  
  setupEventListeners() {
    this.header.addEventListener('click', () => this.toggleExpanded());
    window.addEventListener('resize', () => {
      if (this.isExpanded) {
        this.resizeCanvas();
        this.redraw();
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
      this.resizeCanvas();
      this.redraw();
    } else {
      this.content.classList.remove('expanded');
      this.header.classList.remove('expanded');
      this.toggle.textContent = '▲';
    }
  }
  
  resizeCanvas() {
    // Resize canvas to fit container
    const rect = this.content.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    
    // Get actual displayed dimensions
    const displayWidth = this.content.clientWidth;
    const displayHeight = this.content.clientHeight;
    
    // Set canvas resolution
    this.canvas.width = displayWidth * dpr;
    this.canvas.height = displayHeight * dpr;
    
    // Scale context
    this.ctx.scale(dpr, dpr);
    
    // Ensure canvas fills container
    this.canvas.style.width = displayWidth + 'px';
    this.canvas.style.height = displayHeight + 'px';
  }
  
  async fetchSpectrumData() {
    try {
      const response = await fetch(`/api/spectrum-data?target=${this.currentTarget}&minutes=60`);
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
      if (now - this.lastUpdate > 3000) { // Update every 3 seconds
        this.updateSpectrum();
        this.lastUpdate = now;
      }
    }, 1000);
  }
  
  async updateSpectrum() {
    if (!this.isExpanded) return;
    
    const newData = await this.fetchSpectrumData();
    if (!newData || !newData.data || newData.data.length === 0) {
      return;
    }
    
    this.spectrumData = newData;
    this.redraw();
  }
  
  redraw() {
    if (!this.ctx || !this.spectrumData) return;
    
    const spectrum = this.spectrumData;
    const width = this.canvas.width;
    const height = this.canvas.height;
    const numBins = spectrum.bins.length;
    
    if (numBins === 0 || spectrum.data.length === 0) {
      this.drawEmpty();
      return;
    }
    
    // Clear canvas with dark background
    this.ctx.fillStyle = '#050a15';
    this.ctx.fillRect(0, 0, width, height);
    
    // Draw spectrum rows
    const rowHeight = Math.max(2, Math.floor(height / Math.max(1, spectrum.data.length)));
    
    for (let rowIdx = 0; rowIdx < spectrum.data.length; rowIdx++) {
      const row = spectrum.data[rowIdx];
      const powers = row.powers || [];
      const y = rowIdx * rowHeight;
      
      // Draw each frequency bin
      const binWidth = width / numBins;
      
      for (let binIdx = 0; binIdx < numBins; binIdx++) {
        const power = powers[binIdx] || 0;
        const colorKey = Math.min(10, Math.max(0, Math.round(power)));
        this.ctx.fillStyle = this.colorMap[colorKey];
        this.ctx.fillRect(binIdx * binWidth, y, binWidth + 1, rowHeight + 1);
      }
    }
    
    // Draw grid overlay
    this.drawGridOverlay(spectrum);
  }
  
  drawGridOverlay(spectrum) {
    const width = this.canvas.width;
    const height = this.canvas.height;
    const numBins = spectrum.bins.length;
    
    // Draw frequency bin grid on X-axis
    const binWidth = width / numBins;
    const freqMin = spectrum.range.min;
    const freqMax = spectrum.range.max;
    
    // Draw vertical grid lines and frequency labels
    this.ctx.strokeStyle = 'rgba(255,255,255,0.1)';
    this.ctx.lineWidth = 1;
    this.ctx.font = '10px monospace';
    this.ctx.fillStyle = '#9aa3c7';
    this.ctx.textAlign = 'center';
    
    const step = Math.max(1, Math.round((freqMax - freqMin) / 4));
    for (let f = Math.ceil(freqMin / step) * step; f <= freqMax; f += step) {
      const ratio = (f - freqMin) / (freqMax - freqMin);
      const x = ratio * width;
      this.ctx.beginPath();
      this.ctx.moveTo(x, 0);
      this.ctx.lineTo(x, height - 15);
      this.ctx.stroke();
      this.ctx.fillText(f.toFixed(0), x, height - 2);
    }
  }
  
  setTarget(target) {
    if (target !== this.currentTarget) {
      this.currentTarget = target;
      this.pixelBuffer = [];
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

document.addEventListener('DOMContentLoaded', () => {
  spectrumWaterfall = new SpectrumWaterfall();
});
