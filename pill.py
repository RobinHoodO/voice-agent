#!/usr/bin/env python3
"""Floating Siri-wave presence for live conversation mode.

A small borderless, click-through panel at the bottom-center of the active screen,
hosting the 21st.dev "siri-wave" GLSL component (ported verbatim) in a WKWebView.

State is carried by color: listening shows the wave's default chromatic look;
thinking recolors to periwinkle, speaking to amber (a tint uniform added to the
shader). The wave amplitude also reacts to live mic level (iAudio uniform, fed by
agent.py from the conversation's mic stream) so it feels like it's taking words in.

AppKit is not thread-safe — every method here MUST be called on the main (rumps)
thread. agent.py drives it from its Timer ticks, never from worker threads.
"""
from AppKit import (
    NSPanel, NSColor, NSScreen, NSEvent, NSMakeRect,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSStatusWindowLevel, NSBackingStoreBuffered,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
)
from WebKit import WKWebView, WKWebViewConfiguration

PANEL_W = 360
PANEL_H = 156

# state -> (tint rgb 0..1, tint amount 0..1). listening = no tint (default chromatic
# wave); thinking = periwinkle, speaking = amber — matching the prior orb's palette.
STATE_TINT = {
    "listening": ((1.0, 1.0, 1.0), 0.0),       # default chromatic — taking you in
    "thinking":  ((0.486, 0.549, 1.000), 0.85),  # periwinkle
    "acting":    ((0.239, 0.824, 0.753), 0.85),  # teal — running a tool/command
    "speaking":  ((1.000, 0.698, 0.239), 0.85),  # amber — answering
}


def _active_screen():
    m = NSEvent.mouseLocation()
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= m.x <= f.origin.x + f.size.width and
                f.origin.y <= m.y <= f.origin.y + f.size.height):
            return s
    return NSScreen.mainScreen()


# The 21st.dev wave fragment shader, verbatim, with added uniforms: uTintColor/
# uTintAmount (state recolor) and iAudio (live mic level scales the amplitude).
_HTML = '''<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;background:transparent;overflow:hidden}
  #c{display:block;width:''' + str(PANEL_W) + '''px;height:''' + str(PANEL_H) + '''px;
     background:transparent}
</style></head><body>
<canvas id="c"></canvas>
<script>
const VERT = `attribute vec2 aPos; void main(){ gl_Position=vec4(aPos,0.0,1.0); }`;
const FRAG = `precision highp float;
uniform vec2 iResolution; uniform float iTime;
uniform vec3 uTintColor; uniform float uTintAmount; uniform float iAudio;
const float PI = 3.14159265359;
const float AMPLITUDE   = 0.32;
const float FREQ        = 1.1;
const float ABER_FREQ   = 1.0;
const float SPEED       = 2.4;
const float WAVE_SCALE  = 0.6;
const float ABERRATION  = 2.6;
const float THICKNESS   = 3.0;
const float INTENSITY   = 2.;
const float FALLOFF     = 1.7;
const float EDGE_MASK   = 0.4;
const float EDGE_INSET  = 0.0;
const float BAND_FILL   = 30000.0;
const float BAND_THICK  = 0.08;
const float SOFTNESS    = 2.5;
const float LOW_AMP     = 6.0;
const float LOW_INT     = 1.5;
const float MID_ABER    = 0.8;
const float MID_ABAMP   = 0.05;
const float MID_BAND    = 20.0;
const float MID_SOFT    = 0.4;
const float HIGH_ABER   = 0.5;
const float HIGH_ABAMP  = 0.06;
const float RESOLVED    = 1.0;
const float UNRES_SCALE = 0.14;

vec3 spectral4(int s){
    float x = float(s);
    return clamp(vec3(abs(x-3.0)-1.0, 2.0-abs(x-2.0), 2.0-abs(x-4.0)), 0.0, 1.0);
}

void mainImage(out vec4 fragColor, in vec2 fragCoord){
    vec2 R = iResolution.xy;
    float aspect = R.x / R.y;
    vec2 p = (fragCoord + 0.5) * 2.0 / R - 1.0;
    p.x *= aspect;
    float yScreen = p.y;
    p /= max(WAVE_SCALE, 0.1);

    float t   = iTime;
    // live mic level scales the wave: a calm floor when quiet, swelling with speech.
    float aud = 0.35 + 1.15 * clamp(iAudio, 0.0, 1.0);
    float low  = clamp(0.45 + 0.45*sin(t*0.8)*sin(t*0.37+1.0), 0.0, 1.0);
    float mid  = clamp(0.40 + 0.40*sin(t*1.7+2.0)*sin(t*0.53), 0.0, 1.0);
    float high = clamp(0.30 + 0.30*sin(t*2.9+4.0)*sin(t*0.71+2.0), 0.0, 1.0);

    float res   = clamp(RESOLVED, 0.0, 1.0);
    float drift = mod(t, 20.0*PI) * SPEED;

    float xN  = p.x / max(aspect, 1.0);
    float env = cos(PI*0.5 * min(abs(0.9*xN), 1.0));
    env *= env;

    float A1    = (AMPLITUDE + 0.01*low*LOW_AMP) * aud;
    float A2    = A1 + mid*MID_ABAMP + high*HIGH_ABAMP;
    float AB    = (ABERRATION + mid*MID_ABER + high*HIGH_ABER)*res;
    float th    = mix(0.1, 0.01*THICKNESS, res);
    float inten = mix(0.1, 0.01*(INTENSITY + low*LOW_INT), res);
    float soft  = 0.01*res*max(0.0, SOFTNESS + mid*MID_SOFT);

    float dUnres = max(length(p) - mix(0.14, UNRES_SCALE, res), 0.0);
    float yMain = A1 * env * res * sin(p.x*FREQ + drift);

    float bandFillTh = max(BAND_THICK, 1e-4);
    float bandAmt    = 1e-4 * BAND_FILL * inten;
    vec3 num = vec3(0.0), den = vec3(0.0);
    for(int s = 0; s < 4; s++){
        vec3 hue = mix(vec3(1.0), spectral4(s), res);
        den += hue;
        float ab = mix(-AB, AB, float(s)/3.0);
        float yL = A2 * env * res * sin(p.x*ABER_FREQ + drift + ab);
        float d   = mix(dUnres, abs(p.y - yL), res);
        float lor = mix(1.0/(1.0 + (0.02*d)*(0.02*d)), 1.0, res);
        float line = inten / (sqrt(d*d + soft*soft) + th);
        float lo = min(yMain, yL), hi = max(yMain, yL);
        float dBand = max(0.0, max(p.y - hi, lo - p.y));
        float band  = bandAmt / (dBand + bandFillTh);
        num += hue * lor * (line + band);
    }
    vec3 col = num / den;

    float dM    = mix(dUnres, abs(p.y - yMain), res);
    float lorM  = mix(1.0/(1.0 + (0.02*dM)*(0.02*dM)), 1.0, res);
    float boost = (1.0 - res) * (14.0*low + 4.0);
    col += 0.5 * inten * (lorM + boost) / (sqrt(dM*dM + soft*soft) + th);

    col = pow(max(col, 0.0), vec3(1.5));
    float emT = clamp((abs(yScreen) - 1.0 + EDGE_INSET) / (-max(EDGE_MASK, 1e-4)), 0.0, 1.0);
    float em  = emT*emT*(3.0 - 2.0*emT);
    float gauss = exp(-pow(xN*FALLOFF, 2.0));
    col *= mix(1.0, em*gauss, res);
    col *= res;

    // state recolor: keep the wave's brightness, push hue toward the state tint.
    float lum = max(col.r, max(col.g, col.b));
    col = mix(col, lum * uTintColor, uTintAmount);

    // transparent background: alpha = wave brightness, so only the glow shows over
    // whatever is behind the panel. Premultiplied output (col*a) so low-alpha edges
    // carry no dark colour — kills the black halo/outline.
    float a = clamp(max(col.r, max(col.g, col.b)), 0.0, 1.0);
    fragColor = vec4(col * a, a);
}
void main(){ mainImage(gl_FragColor, gl_FragCoord.xy); }`;

const canvas = document.getElementById('c');
const gl = canvas.getContext('webgl', {alpha:true, premultipliedAlpha:true});

function compile(type, src){
  const sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh);
  if(!gl.getShaderParameter(sh, gl.COMPILE_STATUS)){ console.error(gl.getShaderInfoLog(sh)); }
  return sh;
}
const prog = gl.createProgram();
gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FRAG));
gl.linkProgram(prog); gl.useProgram(prog);

const buf = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, buf);
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
const aPos = gl.getAttribLocation(prog, "aPos");
gl.enableVertexAttribArray(aPos);
gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

const uRes = gl.getUniformLocation(prog, "iResolution");
const uTime = gl.getUniformLocation(prog, "iTime");
const uTintColor = gl.getUniformLocation(prog, "uTintColor");
const uTintAmount = gl.getUniformLocation(prog, "uTintAmount");
const uAudio = gl.getUniformLocation(prog, "iAudio");

const dpr = Math.min(window.devicePixelRatio || 1, 2);
const W = ''' + str(PANEL_W) + ''', H = ''' + str(PANEL_H) + ''';
canvas.width = Math.round(W*dpr); canvas.height = Math.round(H*dpr);
gl.viewport(0, 0, canvas.width, canvas.height);

let tintColor = [1,1,1], tintAmount = 0.0;
window.setState = function(s){
  if(s === 'thinking'){ tintColor = [0.486,0.549,1.0]; tintAmount = 0.85; }
  else if(s === 'acting'){ tintColor = [0.239,0.824,0.753]; tintAmount = 0.85; }
  else if(s === 'speaking'){ tintColor = [1.0,0.698,0.239]; tintAmount = 0.85; }
  else { tintColor = [1,1,1]; tintAmount = 0.0; }   // listening = default chromatic
};

// live mic level, fed from Python. Smoothed here so the wave breathes, not jitters.
let levelTarget = 0.0, level = 0.0;
window.setLevel = function(x){ levelTarget = Math.max(0, Math.min(1, x)); };

const start = performance.now();
function frame(){
  const t = (performance.now() - start) / 1000;
  level += (levelTarget - level) * 0.25;   // ~exponential smoothing toward target
  gl.uniform2f(uRes, canvas.width, canvas.height);
  gl.uniform1f(uTime, t);
  gl.uniform3f(uTintColor, tintColor[0], tintColor[1], tintColor[2]);
  gl.uniform1f(uTintAmount, tintAmount);
  gl.uniform1f(uAudio, level);
  gl.drawArrays(gl.TRIANGLES, 0, 3);
  requestAnimationFrame(frame);
}
frame();
</script></body></html>'''


class Pill:
    def __init__(self):
        self.panel = None
        self.web = None
        self._state = "listening"

    def _build(self) -> None:
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)          # no drop shadow → no dark outline around the wave
        panel.setIgnoresMouseEvents_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)

        cfg = WKWebViewConfiguration.alloc().init()
        web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), cfg)
        # transparent so the canvas's rounded corners aren't boxed in black
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        web.setOpaque_(False)
        web.setWantsLayer_(True)
        try:
            web.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        except Exception:
            pass
        web.loadHTMLString_baseURL_(_HTML, None)
        panel.setContentView_(web)

        self.panel = panel
        self.web = web

    def _js(self, code: str) -> None:
        if self.web is not None:
            try:
                self.web.evaluateJavaScript_completionHandler_(code, None)
            except Exception:
                pass

    def set_state(self, state: str) -> None:
        self._state = state if state in STATE_TINT else "listening"
        self._js(f"window.setState && window.setState('{self._state}')")

    def set_level(self, level: float) -> None:
        """Feed the live mic level (0..1) into the wave. Main-thread only."""
        try:
            self._js(f"window.setLevel && window.setLevel({float(level):.3f})")
        except Exception:
            pass

    def show(self, state: str = "listening") -> None:
        if self.panel is None:
            self._build()
        vf = _active_screen().visibleFrame()
        self.panel.setFrameOrigin_((vf.origin.x + (vf.size.width - PANEL_W) / 2.0,
                                    vf.origin.y + 28))
        self.set_state(state)
        self.panel.orderFrontRegardless()

    def hide(self) -> None:
        if self.panel is not None:
            self.panel.orderOut_(None)
