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
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    this.ctx.scale(dpr, dpr);
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
    
    // Extract just the latest powers row if we have new data
    if (newData.data.length > 0) {
      const latestRow = newData.data[newData.data.length - 1];
      this.pixelBuffer.push(latestRow.powers);
      
      // Keep buffer size reasonable
      const maxRows = Math.ceil(this.canvas.height / this.gridPixels);
      if (this.pixelBuffer.length > maxRows) {
        this.pixelBuffer.shift();
      }
    }
    
    this.redraw();
  }
  
  redraw() {
    if (!this.ctx || !this.spectrumData) return;
    
    const spectrum = this.spectrumData;
    const width = this.canvas.width;
    const height = this.canvas.height;
    const numBins = spectrum.bins.length;
    
    if (numBins === 0) {
      this.drawEmpty();
      return;
    }
    
    // Clear canvas
    this.ctx.fillStyle = '#050a15';
    this.ctx.fillRect(0, 0, width, height);
    
    // Draw grid and frequency labels
    this.drawGrid(spectrum);
    
    // Draw spectrum data
    this.drawSpectrum(spectrum);
  }
  
  drawEmpty() {
    const width = this.canvas.width;
    const height = this.canvas.height;
    this.ctx.fillStyle = '#050a15';
    this.ctx.fillRect(0, 0, width, height);
    this.ctx.fillStyle = '#9aa3c7';
    this.ctx.font = '12px system-ui';
    this.ctx.textAlign = 'center';
    this.ctx.fillText('Waiting for data...', width / 2, height / 2);
  }
  
  drawGrid(spectrum) {
    const width = this.canvas.width;
    const height = this.canvas.height;
    const numBins = spectrum.bins.length;
    
    // Draw frequency bins on X-axis
    const binWidth = width / numBins;
    const freqMin = spectrum.range.min;
    const freqMax = spectrum.range.max;
    
    // Draw vertical grid lines every ~50 MHz
    this.ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    this.ctx.lineWidth = 1;
    this.ctx.font = '10px monospace';
    this.ctx.fillStyle = '#9aa3c7';
    this.ctx.textAlign = 'center';
    
    const step = Math.max(1, Math.round((freqMax - freqMin) / 4)); // 4-5 labels
    for (let f = Math.ceil(freqMin / step) * step; f <= freqMax; f += step) {
      const ratio = (f - freqMin) / (freqMax - freqMin);
      const x = ratio * width;
      this.ctx.beginPath();
      this.ctx.moveTo(x, 0);
      this.ctx.lineTo(x, height);
      this.ctx.stroke();
      this.ctx.fillText(f.toFixed(0), x, height - 2);
    }
    
    // Draw horizontal grid lines every 20 pixels
    this.ctx.strokeStyle = 'rgba(255,255,255,0.03)';
    for (let y = 0; y < height; y += 20) {
      this.ctx.beginPath();
      this.ctx.moveTo(0, y);
      this.ctx.lineTo(width, y);
      this.ctx.stroke();
    }
  }
  
  drawSpectrum(spectrum) {
    const width = this.canvas.width;
    const height = this.canvas.height;
    const numBins = spectrum.bins.length;
    
    if (numBins === 0) return;
    
    const binWidth = width / numBins;
    
    // Draw all spectrum data
    let y = 0;
    for (const row of spectrum.data) {
      const powers = row.powers;
      for (let binIdx = 0; binIdx < numBins; binIdx++) {
        const power = powers[binIdx] || 0;
        const colorKey = Math.min(10, Math.max(0, Math.round(power)));
        this.ctx.fillStyle = this.colorMap[colorKey];
        this.ctx.fillRect(binIdx * binWidth, y, binWidth, this.gridPixels);
      }
      y += this.gridPixels;
      if (y >= height) break;
    }
    
    // Draw rolling buffer if enabled (optional smooth scrolling)
    // For now, we just redraw all data
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
