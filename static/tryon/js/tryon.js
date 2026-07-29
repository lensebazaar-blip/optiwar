/**
 * Face Measurement v6.5 — Distance-locked iris calibration
 * Forces user to hold phone at optimal distance (~40cm) via visual guide.
 * Locks iris pixel size during scan — rejects frames outside ±8% band.
 * Multi-landmark face width (temples + cheekbones) for stability.
 */
import { FaceLandmarker, FilesetResolver } from 
    "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.18/vision_bundle.mjs";

const IRIS_DIAMETER_MM = 11.7;
const NEAR_PD_FACTOR = 0.96;
const BUFFER_SIZE = 20;
const STABLE_THRESHOLD = 0.7;
const STABLE_FRAMES_NEEDED = 15;
const OUTLIER_SIGMA = 1.8;
const IRIS_LR_TOLERANCE = 0.22;

const TARGET_FACE_RATIO_MIN = 0.30;
const TARGET_FACE_RATIO_MAX = 0.65;
const TARGET_FACE_RATIO_IDEAL = 0.48;
const IRIS_LOCK_TOLERANCE = 0.12;

const PHASES_NEEDED = 2;
const TEMPLE_TABLE = [
    { max: 126, val: 135 }, { max: 132, val: 140 },
    { max: 140, val: 140 }, { max: 148, val: 145 },
    { max: 999, val: 150 }
];

/* --- Real-world frame candidate generator ---
 * Generates 3-4 frame size options ranked by optical suitability.
 * Uses PD + face width + bridge anatomy together (not face width alone).
 * Approximates: Frame Front Width ≈ A + DBL + A + ~10mm hinge allowance
 * Decentration per eye = (Frame PD − Far PD) / 2
 */
function generateFrameCandidates(farPD, faceW, innerDist) {
    // Bridge candidates based on inner eye distance
    const baseBridge = Math.round(innerDist * 0.85);
    const bridgeOptions = [baseBridge - 1, baseBridge, baseBridge + 1, baseBridge + 2]
        .map(b => Math.max(14, Math.min(24, b)))
        .filter((v, i, a) => a.indexOf(v) === i); // unique

    // Temple from face width
    let temple = 140;
    for (const r of TEMPLE_TABLE) { if (faceW < r.max) { temple = r.val; break; } }

    // Target frame front width should approximate face width
    // Frame width ≈ 2×A + DBL + ~10mm (hinge allowance)
    const HINGE_ALLOWANCE = 10;
    const candidates = [];

    for (const br of bridgeOptions) {
        // Derive lens size from face width: A = (faceWidth - bridge - hinge) / 2
        const idealLens = Math.round((faceW - br - HINGE_ALLOWANCE) / 2);
        // Generate lens options around ideal: -2, -1, 0, +1
        const lensOptions = [idealLens - 2, idealLens - 1, idealLens, idealLens + 1]
            .map(l => Math.max(42, Math.min(60, l)))
            .filter((v, i, a) => a.indexOf(v) === i);

        for (const lens of lensOptions) {
            const framePCD = lens + br; // Frame PD
            const approxWidth = (lens * 2) + br + HINGE_ALLOWANCE;
            const dec = (framePCD - farPD) / 2;
            const absDec = Math.abs(dec);
            const widthDiff = Math.abs(approxWidth - faceW);

            // Suitability scoring (lower = better)
            // Penalise decentration >4mm, frame width mismatch >4mm
            let score = absDec * 3 + widthDiff * 1.5;
            if (absDec > 6) score += 20;
            if (absDec > 4) score += 8;
            if (widthDiff > 6) score += 10;

            // Suitability label
            let suitability;
            if (absDec <= 3 && widthDiff <= 3) suitability = 'Excellent';
            else if (absDec <= 4.5 && widthDiff <= 5) suitability = 'Very Good';
            else if (absDec <= 6 && widthDiff <= 7) suitability = 'Good';
            else suitability = 'Acceptable';

            candidates.push({
                lens, bridge: br, temple,
                framePCD, approxWidth: Math.round(approxWidth),
                decentration: Math.round(dec * 10) / 10,
                absDecentration: Math.round(absDec * 10) / 10,
                widthDiff: Math.round(widthDiff),
                suitability, score,
                size: `${lens}-${br}-${temple}`
            });
        }
    }

    // Sort by score, deduplicate by size string, take top 4
    candidates.sort((a, b) => a.score - b.score);
    const seen = new Set();
    const unique = [];
    for (const c of candidates) {
        if (!seen.has(c.size)) { seen.add(c.size); unique.push(c); }
        if (unique.length >= 4) break;
    }
    return unique;
}

let faceLandmarker = null, video, canvas, ctx;
let running = false, measureBuffer = [], stableCount = 0;
let measurementDone = false, finalMeasurements = null;
let phaseResults = [], currentPhase = 0;
let lockedIrisPx = 0;
let scanStartTime = 0;
const SCAN_TIMEOUT_MS = 30000;

const $ = id => document.getElementById(id);

document.addEventListener('DOMContentLoaded', () => {
    video = $('video');
    canvas = $('overlayCanvas');
    ctx = canvas.getContext('2d');
    $('startBtn').onclick = startScan;
    $('saveBtn').onclick = captureAndSave;
    $('rescanBtn').onclick = rescan;
    $('matchBtn').onclick = showMatchingFrames;
    $('browseBtn').onclick = () => { window.location.href = '/categories/Spectacles%20Frame'; };
    $('closeFrames').onclick = () => { $('framesOverlay').classList.remove('show'); };
    $('rescanAgainBtn').onclick = () => { $('returningOverlay').classList.remove('show'); };
    checkExisting();
    initModel();
});

async function initModel() {
    setStatus('Loading AI...');
    try {
        const vision = await FilesetResolver.forVisionTasks(
            "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.18/wasm");
        let dlg = "GPU";
        try {
            faceLandmarker = await FaceLandmarker.createFromOptions(vision, {
                baseOptions: { modelAssetPath: "/static/tryon/models/face_landmarker.task", delegate: "GPU" },
                runningMode: "VIDEO", numFaces: 1, outputFaceBlendshapes: false, outputFacialTransformationMatrixes: false
            });
        } catch (e) {
            dlg = "CPU";
            faceLandmarker = await FaceLandmarker.createFromOptions(vision, {
                baseOptions: { modelAssetPath: "/static/tryon/models/face_landmarker.task", delegate: "CPU" },
                runningMode: "VIDEO", numFaces: 1, outputFaceBlendshapes: false, outputFacialTransformationMatrixes: false
            });
        }
        console.log(`FaceLandmarker (${dlg})`);
        setStatus('Ready');
        $('startBtn').classList.add('ready');
    } catch (e) {
        console.error(e);
        setStatus('Model failed');
    }
}

async function startScan() {
    if (!faceLandmarker) return;
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }
        });
        video.srcObject = stream;
        await video.play();
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        $('startBtn').style.display = 'none';
        $('guideOval').style.display = 'none';
        $('introText').style.display = 'none';
        running = true; measurementDone = false;
        stableCount = 0; measureBuffer = []; finalMeasurements = null;
        phaseResults = []; currentPhase = 0; lockedIrisPx = 0;
        scanStartTime = performance.now();
        hideResults();
        setStatus('Scanning...');
        detectLoop();
    } catch (e) {
        setStatus('Camera denied');
    }
}

function detectLoop() {
    if (!running || measurementDone) return;
    
    // Safety timeout: if scan takes too long, use best available data
    if (scanStartTime > 0 && (performance.now() - scanStartTime) > SCAN_TIMEOUT_MS && measureBuffer.length >= 10) {
        const m = smooth(measureBuffer[measureBuffer.length - 1]);
        if (phaseResults.length === 0) phaseResults.push({...m});
        const final = averagePhases(phaseResults.length >= PHASES_NEEDED ? phaseResults : [...phaseResults, {...m}]);
        done(final);
        return;
    }
    
    const result = faceLandmarker.detectForVideo(video, performance.now());
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    if (result.faceLandmarks && result.faceLandmarks.length > 0) {
        const lm = result.faceLandmarks[0];
        const w = canvas.width, h = canvas.height;
        
        // Step 1: Check distance via face height ratio
        const faceTop = lm[10].y * h;
        const faceBot = lm[152].y * h;
        const faceH = Math.abs(faceBot - faceTop);
        const faceRatio = faceH / h;
        
        const distGuide = $('distGuide');
        if (faceRatio < TARGET_FACE_RATIO_MIN) {
            if (distGuide) { distGuide.textContent = 'Move closer'; distGuide.className = 'dist-guide dist-far'; distGuide.style.display = 'block'; }
            stableCount = Math.max(0, stableCount - 1);
            drawDistanceRing(faceRatio, lm, w, h);
            requestAnimationFrame(detectLoop);
            return;
        }
        if (faceRatio > TARGET_FACE_RATIO_MAX) {
            if (distGuide) { distGuide.textContent = 'Move back'; distGuide.className = 'dist-guide dist-near'; distGuide.style.display = 'block'; }
            stableCount = Math.max(0, stableCount - 1);
            drawDistanceRing(faceRatio, lm, w, h);
            requestAnimationFrame(detectLoop);
            return;
        }
        if (distGuide) { distGuide.style.display = 'none'; }
        
        // Step 2: Calculate measurements
        const raw = calcMeasurements(lm, w, h);
        if (!raw || raw._rejected) {
            stableCount = Math.max(0, stableCount - 1);
            requestAnimationFrame(detectLoop);
            return;
        }
        
        // Step 3: Distance lock — lock iris pixel size after initial frames
        if (lockedIrisPx === 0 && measureBuffer.length >= 5) {
            const irisSizes = measureBuffer.map(m => m._irisPx).sort((a,b) => a-b);
            lockedIrisPx = irisSizes[Math.floor(irisSizes.length / 2)];
        }
        if (lockedIrisPx > 0) {
            const drift = Math.abs(raw._irisPx - lockedIrisPx) / lockedIrisPx;
            if (drift > IRIS_LOCK_TOLERANCE) {
                stableCount = Math.max(0, stableCount - 1);
                requestAnimationFrame(detectLoop);
                return;
            }
        }
        
        const m = smooth(raw);
        drawPDLine(m);
        
        if (measureBuffer.length >= BUFFER_SIZE) {
            const v = variance();
            if (v < STABLE_THRESHOLD) {
                stableCount++;
                const phaseProgress = stableCount / STABLE_FRAMES_NEEDED;
                const totalPct = Math.min(100, Math.round(((currentPhase + phaseProgress) / PHASES_NEEDED) * 100));
                setStatus(`Measuring ${totalPct}%`);
                $('progressRing').style.setProperty('--pct', totalPct);
                if (stableCount >= STABLE_FRAMES_NEEDED) {
                    completePhase(m);
                    return;
                }
            } else { stableCount = Math.max(0, stableCount - 2); }
            if (stableCount < STABLE_FRAMES_NEEDED) {
                const phaseProgress = stableCount / STABLE_FRAMES_NEEDED;
                const totalPct = Math.min(100, Math.round(((currentPhase + phaseProgress) / PHASES_NEEDED) * 100));
                setStatus(`Measuring ${totalPct}%`);
                $('progressRing').style.setProperty('--pct', totalPct);
            }
        }
    } else {
        stableCount = 0;
        setStatus('Position face');
    }
    requestAnimationFrame(detectLoop);
}

function drawDistanceRing(faceRatio, lm, w, h) {
    const cx = (lm[10].x + lm[152].x) / 2 * w;
    const cy = (lm[10].y + lm[152].y) / 2 * h;
    const targetH = TARGET_FACE_RATIO_IDEAL * h;
    ctx.save();
    ctx.strokeStyle = '#f59e0b';
    ctx.lineWidth = 2;
    ctx.setLineDash([8, 6]);
    ctx.beginPath();
    ctx.ellipse(cx, cy, targetH * 0.38, targetH * 0.5, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
}

function completePhase(m) {
    phaseResults.push({...m});
    currentPhase++;
    
    if (currentPhase >= PHASES_NEEDED) {
        // Average all phase results for final measurement
        const final = averagePhases(phaseResults);
        done(final);
    } else {
        // Reset buffer for next phase, keep scanning
        stableCount = 0;
        measureBuffer = [];
        requestAnimationFrame(detectLoop);
    }
}

function averagePhases(phases) {
    const keys = ['farPD','nearPD','faceWidth','eyeMouth','innerDist'];
    const avg = {...phases[0]};
    keys.forEach(k => {
        const vals = phases.map(p => p[k]).filter(v => v !== undefined).sort((a,b) => a-b);
        if (vals.length) avg[k] = vals[Math.floor(vals.length / 2)];
    });
    // Round measurements to 0.5mm precision (optical standard)
    avg.farPD = Math.round(avg.farPD * 2) / 2;
    avg.nearPD = Math.round(avg.nearPD * 2) / 2;
    avg.faceWidth = Math.round(avg.faceWidth * 2) / 2;
    avg.eyeMouth = Math.round(avg.eyeMouth * 2) / 2;
    if (avg.innerDist) avg.innerDist = Math.round(avg.innerDist * 2) / 2;
    // Re-generate frame candidates from averaged measurements
    const innerD = avg.innerDist || (avg.recBridge / 0.85);
    avg.frameCandidates = generateFrameCandidates(avg.farPD, avg.faceWidth, innerD);
    const best = avg.frameCandidates[0] || { lens: 50, bridge: 18, temple: 140, decentration: 0, approxWidth: 128 };
    avg.recDiameter = best.lens;
    avg.recBridge = best.bridge;
    avg.recTemple = best.temple;
    avg.recFrameWidth = best.approxWidth;
    avg.decentration = best.decentration;
    // Confidence: how close were the phase readings?
    const fwSpread = Math.max(...phases.map(p=>p.faceWidth)) - Math.min(...phases.map(p=>p.faceWidth));
    const pdSpread = Math.max(...phases.map(p=>p.farPD)) - Math.min(...phases.map(p=>p.farPD));
    avg._confidence = fwSpread <= 2 && pdSpread <= 1.5 ? 'high' : fwSpread <= 4 ? 'medium' : 'low';
    return avg;
}

function calcMeasurements(lm, w, h) {
    const lp = lm[468], rp = lm[473];
    const nb = lm[6], ch = lm[152];
    
    const lIrisL = lm[469], lIrisT = lm[470], lIrisR = lm[471], lIrisB = lm[472];
    const rIrisL = lm[474], rIrisT = lm[475], rIrisR = lm[476], rIrisB = lm[477];
    const leftIrisH = Math.hypot((lIrisR.x-lIrisL.x)*w, (lIrisR.y-lIrisL.y)*h);
    const leftIrisV = Math.hypot((lIrisB.x-lIrisT.x)*w, (lIrisB.y-lIrisT.y)*h);
    const rightIrisH = Math.hypot((rIrisR.x-rIrisL.x)*w, (rIrisR.y-rIrisL.y)*h);
    const rightIrisV = Math.hypot((rIrisB.x-rIrisT.x)*w, (rIrisB.y-rIrisT.y)*h);
    const leftIrisPx = Math.sqrt(leftIrisH * leftIrisV);
    const rightIrisPx = Math.sqrt(rightIrisH * rightIrisV);
    
    const irisRatio = Math.min(leftIrisPx, rightIrisPx) / Math.max(leftIrisPx, rightIrisPx);
    if (irisRatio < (1 - IRIS_LR_TOLERANCE)) return { _rejected: true };
    
    const avgIrisPx = (leftIrisPx + rightIrisPx) / 2;
    if (avgIrisPx < 8 || avgIrisPx > 80) return { _rejected: true };
    
    const px2mm = avgIrisPx / IRIS_DIAMETER_MM;
    
    const pdPx = Math.hypot((rp.x-lp.x)*w, (rp.y-lp.y)*h);
    const farPD = pdPx / px2mm;
    const nearPD = farPD * NEAR_PD_FACTOR;
    if (farPD < 48 || farPD > 80) return { _rejected: true };
    
    // Multi-landmark face width: blend temples + cheekbones for stability
    const lt = lm[234], rt = lm[454];
    const lc = lm[127], rc = lm[356];
    const templePx = Math.hypot((rt.x-lt.x)*w, (rt.y-lt.y)*h);
    const cheekPx = Math.hypot((rc.x-lc.x)*w, (rc.y-lc.y)*h);
    const templeMM = templePx / px2mm;
    const cheekMM = cheekPx / px2mm;
    
    const cheekToTempleRatio = templeMM / Math.max(cheekMM, 1);
    const boundedRatio = Math.min(Math.max(cheekToTempleRatio, 1.02), 1.12);
    const scaledCheek = cheekMM * boundedRatio;
    const faceW = templeMM * 0.4 + scaledCheek * 0.6;
    
    if (faceW < 100 || faceW > 180) return { _rejected: true };
    
    const emPx = Math.hypot((nb.x-ch.x)*w, (nb.y-ch.y)*h);
    const eyeM = emPx / px2mm;
    
    // Inner eye distance for bridge recommendation
    const innerPx = Math.hypot((lm[133].x-lm[362].x)*w, (lm[133].y-lm[362].y)*h);
    const innerDist = innerPx / px2mm;

    // Generate multiple frame candidates using real-world optical logic
    const candidates = generateFrameCandidates(farPD, faceW, innerDist);
    const best = candidates[0] || { lens: 50, bridge: 18, temple: 140, decentration: 0, approxWidth: 128 };

    return {
        farPD: Math.round(farPD*10)/10, nearPD: Math.round(nearPD*10)/10,
        faceWidth: Math.round(faceW*10)/10, eyeMouth: Math.round(eyeM*10)/10,
        innerDist: Math.round(innerDist*10)/10,
        recDiameter: best.lens, recBridge: best.bridge, recTemple: best.temple,
        recFrameWidth: best.approxWidth, decentration: best.decentration,
        frameCandidates: candidates,
        _lp: {x:lp.x*w, y:lp.y*h}, _rp: {x:rp.x*w, y:rp.y*h},
        _irisPx: avgIrisPx, _irisQuality: irisRatio
    };
}

function smooth(m) {
    measureBuffer.push(m);
    if (measureBuffer.length > BUFFER_SIZE) measureBuffer.shift();
    
    // Outlier rejection: remove readings beyond 2 std devs from median
    const filtered = rejectOutliers(measureBuffer);
    if (filtered.length < 3) return m;
    
    // Weighted average: frames with better iris quality (L/R ratio closer to 1) get more weight
    const weights = filtered.map(f => f._irisQuality || 0.75);
    const totalW = weights.reduce((a,b) => a+b, 0);
    
    const a = {...m};
    ['farPD','nearPD','faceWidth','eyeMouth','innerDist'].forEach(k => {
        const valid = filtered.filter((b,i) => b[k] !== undefined);
        if (valid.length === 0) return;
        const vw = valid.map((b,i) => weights[filtered.indexOf(b)]);
        const tw = vw.reduce((a,b) => a+b, 0);
        a[k] = Math.round((valid.reduce((s,b,i) => s + b[k] * vw[i], 0) / tw)*10)/10;
    });
    // Re-generate candidates from smoothed raw measurements
    const innerD = a.innerDist || (a.recBridge ? a.recBridge / 0.85 : 20);
    a.frameCandidates = generateFrameCandidates(a.farPD, a.faceWidth, innerD);
    const best = a.frameCandidates[0] || m;
    a.recDiameter = best.lens || m.recDiameter;
    a.recBridge = best.bridge || m.recBridge;
    a.recTemple = best.temple || m.recTemple;
    a.recFrameWidth = best.approxWidth || m.recFrameWidth;
    a.decentration = best.decentration !== undefined ? best.decentration : m.decentration;
    return a;
}

function rejectOutliers(buf) {
    if (buf.length < 5) return buf;
    const vals = buf.map(m => m.faceWidth);
    const sorted = [...vals].sort((a,b) => a-b);
    const median = sorted[Math.floor(sorted.length/2)];
    const deviations = vals.map(v => Math.abs(v - median));
    const mad = [...deviations].sort((a,b) => a-b)[Math.floor(deviations.length/2)];
    const threshold = Math.max(mad * OUTLIER_SIGMA, 1.0);
    return buf.filter((m, i) => Math.abs(vals[i] - median) <= threshold);
}

function variance() {
    if (measureBuffer.length < 5) return 999;
    // Use last 10 frames (more data = more reliable variance check)
    const recent = measureBuffer.slice(-10);
    const filtered = rejectOutliers(recent);
    if (filtered.length < 3) return 999;
    // Check variance of both face width AND PD for dual stability
    const fwVals = filtered.map(m => m.faceWidth);
    const pdVals = filtered.map(m => m.farPD);
    const fwMean = fwVals.reduce((a,b)=>a+b,0)/fwVals.length;
    const pdMean = pdVals.reduce((a,b)=>a+b,0)/pdVals.length;
    const fwVar = Math.sqrt(fwVals.reduce((a,b)=>a+Math.pow(b-fwMean,2),0)/fwVals.length);
    const pdVar = Math.sqrt(pdVals.reduce((a,b)=>a+Math.pow(b-pdMean,2),0)/pdVals.length);
    return Math.max(fwVar, pdVar); // both must be stable
}

function drawPDLine(m) {
    if (!m._lp || !m._rp) return;
    const lp = m._lp, rp = m._rp;
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.8)';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(lp.x, lp.y); ctx.lineTo(rp.x, rp.y); ctx.stroke();
    [lp,rp].forEach(p => { ctx.beginPath(); ctx.moveTo(p.x,p.y-6); ctx.lineTo(p.x,p.y+6); ctx.stroke(); });
    ctx.font = "bold 16px -apple-system, sans-serif";
    ctx.textAlign = 'center';
    ctx.fillStyle = '#fff';
    ctx.shadowColor = 'rgba(0,0,0,0.9)'; ctx.shadowBlur = 6;
    ctx.fillText(`${m.farPD} mm`, (lp.x+rp.x)/2, Math.min(lp.y,rp.y)-14);
    ctx.shadowBlur = 0;
    [lp,rp].forEach(p => { ctx.beginPath(); ctx.arc(p.x,p.y,3,0,Math.PI*2); ctx.fillStyle='#34d399'; ctx.fill(); });
    ctx.restore();
}

function done(m) {
    measurementDone = true; running = false; finalMeasurements = m;
    setStatus('Done');
    $('statusBadge').classList.add('done');
    $('progressRing').style.setProperty('--pct', 100);
    const distGuide = $('distGuide');
    if (distGuide) distGuide.style.display = 'none';
    
    $('mFarPD').textContent = m.farPD;
    $('mNearPD').textContent = m.nearPD;
    $('mFaceW').textContent = m.faceWidth;
    $('mSize').textContent = `${m.recDiameter}-${m.recBridge}-${m.recTemple}`;
    
    // Show decentration value
    const dec = Math.abs(m.decentration);
    const decEl = $('mDec');
    if (decEl) {
        decEl.className = 'dec-badge';
        const decText = `Dec: ${m.decentration > 0 ? '+' : ''}${m.decentration}mm`;
        if (dec <= 3) { decEl.textContent = decText + ' · Excellent'; decEl.classList.add('fit-excellent'); }
        else if (dec <= 4.5) { decEl.textContent = decText + ' · Very Good'; decEl.classList.add('fit-excellent'); }
        else if (dec <= 6) { decEl.textContent = decText + ' · Good'; decEl.classList.add('fit-good'); }
        else { decEl.textContent = decText + ' · Acceptable'; decEl.classList.add('fit-fair'); }
    }
    
    const confEl = $('mConf');
    if (confEl && m._confidence) {
        confEl.className = 'conf-badge';
        if (m._confidence === 'high') { confEl.textContent = 'High accuracy'; confEl.classList.add('conf-high'); }
        else if (m._confidence === 'medium') { confEl.textContent = 'Good accuracy'; confEl.classList.add('conf-medium'); }
        else { confEl.textContent = 'Fair accuracy'; confEl.classList.add('conf-low'); }
    }

    // Render frame candidates table
    const candEl = $('frameCandidates');
    if (candEl && m.frameCandidates && m.frameCandidates.length > 0) {
        let html = '';
        m.frameCandidates.forEach((c, i) => {
            const cls = c.suitability === 'Excellent' ? 'cand-excellent' :
                        c.suitability === 'Very Good' ? 'cand-verygood' :
                        c.suitability === 'Good' ? 'cand-good' : 'cand-ok';
            const best = i === 0 ? ' cand-best' : '';
            html += `<div class="cand-row ${cls}${best}">`;
            html += `<span class="cand-size">${c.size}</span>`;
            html += `<span class="cand-width">~${c.approxWidth}mm</span>`;
            html += `<span class="cand-dec">${c.decentration > 0 ? '+' : ''}${c.decentration}</span>`;
            html += `<span class="cand-suit">${c.suitability}</span>`;
            html += `</div>`;
        });
        candEl.innerHTML = html;
        candEl.classList.add('show');
    }

    $('measureCards').classList.add('show');
    $('bottomBar').classList.add('show');
}

async function captureAndSave() {
    if (!finalMeasurements) return;
    const btn = $('saveBtn');
    btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Saving...';
    
    // Try to capture screenshot, but don't fail if canvas is tainted
    let ss = null;
    try {
        const cap = document.createElement('canvas');
        // Use smaller size for mobile to avoid memory issues
        const scale = Math.min(1, 640 / canvas.width);
        cap.width = Math.round(canvas.width * scale);
        cap.height = Math.round(canvas.height * scale);
        const c = cap.getContext('2d');
        c.save(); c.scale(-scale, scale);
        c.drawImage(video, -canvas.width, 0, canvas.width, canvas.height);
        c.restore();
        ss = cap.toDataURL('image/jpeg', 0.7);
    } catch(e) {
        console.warn('Screenshot capture failed:', e);
    }
    
    const m = finalMeasurements;
    try {
        const body = {
            pd_far: m.farPD, pd_near: m.nearPD, face_width: m.faceWidth, eye_mouth: m.eyeMouth,
            recommended_diameter: m.recDiameter, recommended_bridge: m.recBridge,
            recommended_length: m.recTemple,
            decentration: m.decentration,
            frame_candidates: m.frameCandidates ? m.frameCandidates.map(c => ({
                size: c.size, approx_width: c.approxWidth,
                decentration: c.decentration, suitability: c.suitability
            })) : []
        };
        if (ss) body.screenshot = ss;
        
        const r = await fetch('/api/tryon/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const d = await r.json();
        if (d.success) {
            btn.innerHTML = '&#10003; Saved!';
            btn.classList.add('saved');
            localStorage.setItem('ow_face_measured', '1');
            $('choiceBar').classList.add('show');
        } else {
            console.error('Save failed:', d);
            btn.innerHTML = 'Retry Save';
            btn.disabled = false;
        }
    } catch(e) {
        console.error('Save error:', e);
        btn.innerHTML = 'Retry Save';
        btn.disabled = false;
    }
}

function rescan() {
    measurementDone = false; finalMeasurements = null;
    stableCount = 0; measureBuffer = [];
    phaseResults = []; currentPhase = 0; lockedIrisPx = 0;
    hideResults();
    running = true;
    setStatus('Scanning...');
    $('statusBadge').classList.remove('done');
    $('progressRing').style.setProperty('--pct', 0);
    detectLoop();
}

function hideResults() {
    $('measureCards').classList.remove('show');
    $('bottomBar').classList.remove('show');
    $('choiceBar').classList.remove('show');
    $('framesOverlay').classList.remove('show');
    const candEl = $('frameCandidates');
    if (candEl) { candEl.classList.remove('show'); candEl.innerHTML = ''; }
    const btn = $('saveBtn');
    btn.disabled = false; btn.innerHTML = 'Save'; btn.classList.remove('saved');
    $('matchBtn').disabled = false; $('matchBtn').textContent = 'Show Frames matching my face';
    const distGuide = $('distGuide');
    if (distGuide) distGuide.style.display = 'none';
}

async function showMatchingFrames() {
    $('matchBtn').disabled = true; $('matchBtn').textContent = 'Loading...';
    try {
        const r = await fetch('/api/tryon/matching-frames');
        const d = await r.json();
        if (d.error) { alert(d.error); $('matchBtn').disabled=false; $('matchBtn').textContent='Show Frames matching my face'; return; }
        renderFrames(d);
    } catch(e) { $('matchBtn').disabled=false; $('matchBtn').textContent='Show Frames matching my face'; }
}

function renderFrames(data) {
    const el = $('framesOverlay');
    let html = `<div class="fo-header">
        <h3>Frames for You</h3>
        <p>Size: <strong>${data.measurements.recommended_size}</strong> · ${data.total} match${data.total!==1?'es':''}</p>
    </div><div class="fo-grid">`;
    if (data.frames.length === 0) {
        html += `<div class="fo-empty"><p>No matches in stock</p><a href="/categories/Spectacles%20Frame">Browse all →</a></div>`;
    } else {
        data.frames.forEach(f => {
            const fc = f.fit==='Perfect'?'tag-perfect':f.fit==='Good'?'tag-good':'tag-fair';
            // Recommendation cards: single image (no gallery) -> primary media,
            // rendered via the shared server-owned <picture> renderer. All lazy
            // (this overlay opens on user action, so nothing is above the fold).
            let imgHtml;
            if (f.media && window.owPictureHTML) {
                imgHtml = window.owPictureHTML(f.media, {
                    alt: f.name, width: 220, height: 220,
                    sizes: '(max-width:600px) 46vw, 220px', eager: false
                });
            } else {
                const d = document.createElement('div'); d.textContent = f.name || '';
                imgHtml = `<img src="${f.image}" alt="${d.innerHTML}" loading="lazy" width="220" height="220">`;
            }
            html += `<a href="/categories/Spectacles%20Frame/optiwar?pid=${f.id}" class="fo-card">
                <div class="fo-img">${imgHtml}<span class="fo-tag ${fc}">${f.fit}</span></div>
                <div class="fo-info"><span class="fo-name">${f.name}</span><span class="fo-size">${f.size}</span>
                ${f.price?`<span class="fo-price">₹${f.price.toLocaleString()}</span>`:''}</div></a>`;
        });
    }
    html += `</div>`;
    $('framesContent').innerHTML = html;
    el.classList.add('show');
}

function setStatus(t) {
    const b = $('statusBadge'); if(b) b.querySelector('.sb-text').textContent = t;
}

async function checkExisting() {
    try {
        const r = await fetch('/api/tryon/my-measurements');
        const d = await r.json();
        if (d.has_measurements) {
            $('existingInfo').style.display = 'flex';
            $('exVals').textContent = `PD ${d.pd_far}mm · Size ${d.recommended_size}`;
            $('returningVals').textContent = `Your last measurement: PD ${d.pd_far}mm · Face Width ${d.face_width}mm · Size ${d.recommended_size}`;
            $('returningOverlay').classList.add('show');
        }
    } catch(e) {}
}
