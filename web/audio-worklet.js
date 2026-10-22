class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = 48000;
    this.left = new Float32Array(this.capacity);
    this.right = new Float32Array(this.capacity);
    this.read = 0;
    this.write = 0;
    this.count = 0;
    this.started = false;
    this.startFrames = 48000 * 0.06;
    this.maxFrames = 48000 * 0.25;
    this.port.onmessage = (e) => this.onData(e.data);
  }

  onData(data) {
    if (data && data.reset) {
      this.read = this.write = this.count = 0;
      this.started = false;
      return;
    }
    if (data && data.l) {
      for (let i = 0; i < data.l.length; i++) this.push(data.l[i], data.r[i]);
    } else {
      const pcm = new Int16Array(data);
      for (let i = 0; i < pcm.length >> 1; i++) this.push(pcm[2 * i] / 32768, pcm[2 * i + 1] / 32768);
    }
    if (this.count > this.maxFrames) {
      const drop = this.count - this.startFrames;
      this.read = (this.read + drop) % this.capacity;
      this.count -= drop;
    }
  }

  push(l, r) {
    this.left[this.write] = l;
    this.right[this.write] = r;
    this.write = (this.write + 1) % this.capacity;
    if (this.count < this.capacity) this.count++;
    else this.read = (this.read + 1) % this.capacity;
  }

  process(inputs, outputs) {
    const out = outputs[0];
    const l = out[0], r = out[1] || out[0];
    if (!this.started && this.count >= this.startFrames) this.started = true;
    for (let i = 0; i < l.length; i++) {
      if (this.started && this.count > 0) {
        l[i] = this.left[this.read];
        r[i] = this.right[this.read];
        this.read = (this.read + 1) % this.capacity;
        this.count--;
      } else {
        l[i] = r[i] = 0;
        this.started = false;
      }
    }
    return true;
  }
}

registerProcessor('pcm-player', PcmPlayer);
