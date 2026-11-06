"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
    getAclPoseExtraction,
    getCareEpisode,
    issuePatientStreamTicket,
    getExerciseCatalogItem,
    listAclKneeStiffnessVideos,
    listEpisodeRehabSessions,
    summarizeEpisodeRehabSession,
    updateEpisodeRehabSession,
    type DemoPoseExtraction,
    type DemoPoseFrame,
    type DemoVideo,
    type EpisodeRehabSession,
    type ExerciseCatalogItem,
} from "@rehab/shared/api";
import { loadAuth } from "@rehab/shared/auth";
import { getApiBase } from "@rehab/shared/runtime";
import { connectPatientStream, type PatientScorePayload } from "@rehab/shared/ws";

type PoseLandmark = { x: number; y: number; z: number; visibility?: number };
type VideoFrameMetadata = { mediaTime?: number };
type VideoFrameVideo = HTMLVideoElement & {
    requestVideoFrameCallback?: (callback: (now: number, metadata: VideoFrameMetadata) => void) => number;
    cancelVideoFrameCallback?: (handle: number) => void;
};
type SessionReferenceVideo = DemoVideo & { catalogOnly?: boolean; onlineVideoUrl?: string };

type PoseLandmarker = {
    detectForVideo: (video: HTMLVideoElement, timestamp: number) => { landmarks?: PoseLandmark[][] };
};

type VisionTasksModule = {
    FilesetResolver: {
        forVisionTasks: (basePath: string) => Promise<unknown>;
    };
    PoseLandmarker: {
        createFromOptions: (filesetResolver: unknown, options: Record<string, unknown>) => Promise<PoseLandmarker>;
    };
};

const VISION_TASKS_URL = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.22-rc.20250304/+esm";
const VISION_WASM_BASE = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.22-rc.20250304/wasm";

const importVisionTasks = new Function("url", "return import(url)") as (url: string) => Promise<VisionTasksModule>;

const POSE_CONNECTIONS: Array<[number, number]> = [
    [11, 12],
    [11, 13],
    [13, 15],
    [12, 14],
    [14, 16],
    [11, 23],
    [12, 24],
    [23, 24],
    [23, 25],
    [25, 27],
    [27, 29],
    [29, 31],
    [24, 26],
    [26, 28],
    [28, 30],
    [30, 32],
];

function mediaUrl(url?: string | null) {
    if (!url) return "";
    if (url.startsWith("//")) return `https:${url}`;
    if (url.startsWith("http")) return url;
    return `${getApiBase()}${url}`;
}

function catalogFallbackVideo(exercise: ExerciseCatalogItem): SessionReferenceVideo {
    return {
        exercise_id: exercise.exercise_id,
        slug: exercise.slug,
        title: exercise.title,
        source_url: exercise.source_url,
        video_url: exercise.video_url,
        local_video_path: null,
        public_video_url: null,
        demo_profile: "catalog_fallback",
        relevance_rank: 999,
        relevance_score: 0,
        relevance_notes: exercise.introduction,
        download_status: "not_downloaded",
        pose_status: "not_extracted",
        metadata: { video_provider: exercise.video_provider, video_id: exercise.video_id },
        pose_summary: null,
        catalogOnly: true,
        onlineVideoUrl: mediaUrl(exercise.video_url),
    };
}

function parseExerciseIds(search: string) {
    const params = new URLSearchParams(search);
    const exerciseIds = (params.get("exercise_ids") || "").split(",").map((item) => item.trim()).filter(Boolean);
    return {
        episodeId: params.get("episode_id") || "",
        sessionId: params.get("session_id") || "",
        exerciseIds,
    };
}

function drawPose(canvas: HTMLCanvasElement, video: HTMLVideoElement, landmarks: PoseLandmark[], objectFit: "contain" | "cover") {
    const videoWidth = video.videoWidth || 640;
    const videoHeight = video.videoHeight || 480;
    const width = video.clientWidth || videoWidth;
    const height = video.clientHeight || videoHeight;
    const dpr = window.devicePixelRatio || 1;
    const targetWidth = Math.round(width * dpr);
    const targetHeight = Math.round(height * dpr);
    if (canvas.width !== targetWidth) canvas.width = targetWidth;
    if (canvas.height !== targetHeight) canvas.height = targetHeight;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const scale = objectFit === "contain" ? Math.min(width / videoWidth, height / videoHeight) : Math.max(width / videoWidth, height / videoHeight);
    const renderedWidth = videoWidth * scale;
    const renderedHeight = videoHeight * scale;
    const offsetX = (width - renderedWidth) / 2;
    const offsetY = (height - renderedHeight) / 2;
    const pointX = (point: PoseLandmark) => offsetX + point.x * renderedWidth;
    const pointY = (point: PoseLandmark) => offsetY + point.y * renderedHeight;

    ctx.lineWidth = Math.max(3, width / 180);
    ctx.lineCap = "round";

    for (const [a, b] of POSE_CONNECTIONS) {
        const first = landmarks[a];
        const second = landmarks[b];
        if (!first || !second) continue;
        if ((first.visibility ?? 1) < 0.35 || (second.visibility ?? 1) < 0.35) continue;
        ctx.strokeStyle = "rgba(20, 184, 166, 0.92)";
        ctx.beginPath();
        ctx.moveTo(pointX(first), pointY(first));
        ctx.lineTo(pointX(second), pointY(second));
        ctx.stroke();
    }

    for (const point of landmarks) {
        if ((point.visibility ?? 1) < 0.35) continue;
        ctx.fillStyle = "rgba(249, 115, 22, 0.95)";
        ctx.beginPath();
        ctx.arc(pointX(point), pointY(point), Math.max(4, width / 160), 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
    }
}

function nearestPoseFrame(frames: DemoPoseFrame[], currentTime: number) {
    const detectedFrames = frames.filter((frame) => frame.landmarks?.length);
    if (!detectedFrames.length) return null;
    let nearest = detectedFrames[0];
    let nearestDistance = Math.abs((nearest.timestamp_seconds ?? 0) - currentTime);
    for (const frame of detectedFrames) {
        const distance = Math.abs((frame.timestamp_seconds ?? 0) - currentTime);
        if (distance < nearestDistance) {
            nearest = frame;
            nearestDistance = distance;
        }
    }
    return nearest;
}

function trackingState(status: string, landmarkCount: number, modelStatus: string) {
    if (status === "Starting") return { label: modelStatus === "Loading" ? "Loading tracking" : "Starting camera", dot: "bg-mint-stroke motion-safe:animate-pulse" };
    if (status === "Live") return landmarkCount ? { label: "Tracking you", dot: "bg-mint-stroke" } : { label: "Step into view", dot: "bg-amber-500" };
    if (status === "Detection error") return { label: "Tracking stopped", dot: "bg-rose-600" };
    return { label: "Camera off", dot: "bg-slate-400" };
}

// Short, actionable versions of camera and tracking failures.
function friendlyError(message: string) {
    if (/permission|notallowed|denied/i.test(message)) return "Camera access is blocked. Allow the camera for this site in your browser, then press Start.";
    if (/requested device not found|notfound|no camera/i.test(message)) return "No camera found. Connect one, then press Start.";
    if (/could not start|notreadable|in use/i.test(message)) return "Your camera is busy in another app. Close it there, then press Start.";
    if (/import|module|wasm|mediapipe|\.task/i.test(message)) return "Movement tracking couldn't load. Check your connection and press Start again.";
    if (/detection/i.test(message)) return "Tracking stopped. Press Stop, then Start to try again.";
    if (/failed to fetch|network/i.test(message)) return "Couldn't reach RehabFlow. Check your connection and reload the page.";
    return message;
}

export default function RehabSessionPage() {
    const videoRef = useRef<HTMLVideoElement | null>(null);
    const canvasRef = useRef<HTMLCanvasElement | null>(null);
    const sourceVideoRef = useRef<HTMLVideoElement | null>(null);
    const sourceCanvasRef = useRef<HTMLCanvasElement | null>(null);
    const wsRef = useRef<WebSocket | null>(null);
    const sessionAttemptRef = useRef(0);
    const rafRef = useRef<number | null>(null);
    const videoFrameCallbackRef = useRef<number | null>(null);
    const sourceRafRef = useRef<number | null>(null);
    const poseLandmarkerRef = useRef<PoseLandmarker | null>(null);
    const lastSentAtRef = useRef<number>(0);
    const lastVideoTimeRef = useRef<number>(-1);
    const lastDetectorTimestampRef = useRef<number>(-1);
    const detectionActiveRef = useRef<boolean>(false);
    const patientNotesRef = useRef<string>("");

    const [patientId, setPatientId] = useState<string>("");
    const [episodeId, setEpisodeId] = useState<string>("");
    const [sessionId, setSessionId] = useState<string>("");
    const [requestedExerciseIds, setRequestedExerciseIds] = useState<string[]>([]);
    const [status, setStatus] = useState<string>("Ready");
    const [modelStatus, setModelStatus] = useState<string>("Not loaded");
    const [score, setScore] = useState<number>(0);
    const [landmarkCount, setLandmarkCount] = useState<number>(0);
    const [videos, setVideos] = useState<SessionReferenceVideo[]>([]);
    const [selectedId, setSelectedId] = useState<string>("");
    const [poseDetail, setPoseDetail] = useState<DemoPoseExtraction | null>(null);
    const [error, setError] = useState<string>("");
    const [scoringNotice, setScoringNotice] = useState<string>("");
    const [rehabSession, setRehabSession] = useState<EpisodeRehabSession | null>(null);
    const [patientNotes, setPatientNotes] = useState<string>("");
    const [sessionLoading, setSessionLoading] = useState<boolean>(false);
    const [sessionBusy, setSessionBusy] = useState<boolean>(false);
    const [sessionError, setSessionError] = useState<string>("");
    const [sessionStatus, setSessionStatus] = useState<string>("");
    // Safety gate: an episode that still needs triage must not open guided exercise.
    const [gateBlocked, setGateBlocked] = useState<boolean>(false);
    const hasRealSessionContext = Boolean(episodeId && sessionId);

    const updatePatientNotes = (notes: string) => {
        patientNotesRef.current = notes;
        setPatientNotes(notes);
    };

    useEffect(() => {
        const auth = loadAuth();
        if (auth?.role === "patient") setPatientId(auth.user_id);
        const parsed = parseExerciseIds(window.location.search);
        setEpisodeId(parsed.episodeId);
        setSessionId(parsed.sessionId);
        setRequestedExerciseIds(parsed.exerciseIds);
        if (parsed.episodeId && auth?.role === "patient" && auth.access_token) {
            getCareEpisode(parsed.episodeId, auth.access_token)
                .then((episode) => setGateBlocked(episode.safety_gate_status === "needs_triage"))
                .catch(() => undefined);
        }
    }, []);

    useEffect(() => {
        let cancelled = false;
        if (!hasRealSessionContext) {
            setRehabSession(null);
            updatePatientNotes("");
            setSessionError("");
            setSessionStatus("");
            setSessionLoading(false);
            return;
        }

        const auth = loadAuth();
        if (!auth || auth.role !== "patient") {
            setSessionError("Log in as a patient to update this rehab session.");
            setSessionLoading(false);
            return;
        }

        setSessionLoading(true);
        setSessionError("");
        listEpisodeRehabSessions(episodeId, auth.access_token)
            .then((sessions) => {
                if (cancelled) return;
                const requestedSession = sessions.find((candidate) => candidate.session_id === sessionId);
                if (!requestedSession) throw new Error("This rehab session is unavailable.");
                setRehabSession(requestedSession);
                updatePatientNotes(requestedSession.patient_notes);
            })
            .catch((err) => {
                if (!cancelled) setSessionError((err as Error).message);
            })
            .finally(() => {
                if (!cancelled) setSessionLoading(false);
            });

        return () => {
            cancelled = true;
        };
    }, [episodeId, hasRealSessionContext, sessionId]);

    useEffect(() => {
        let cancelled = false;
        const auth = loadAuth();
        const loadReferences = async () => {
            try {
                const loadDemoVideos = listAclKneeStiffnessVideos;
                const demoResponse = await loadDemoVideos();
                const requested = requestedExerciseIds.length ? new Set(requestedExerciseIds) : null;
                const matchingDemoVideos = demoResponse.videos.filter((video) => !requested || requested.has(video.exercise_id));
                let catalogFallbackVideos: SessionReferenceVideo[] = [];
                if (auth?.role === "patient" && requestedExerciseIds.length) {
                    const demoIds = new Set(matchingDemoVideos.map((video) => video.exercise_id));
                    const missingIds = requestedExerciseIds.filter((id) => !demoIds.has(id));
                    const catalogExercises = await Promise.all(missingIds.map((id) => getExerciseCatalogItem(id, auth.access_token).catch(() => null)));
                    catalogFallbackVideos = catalogExercises.filter((exercise): exercise is ExerciseCatalogItem => Boolean(exercise)).map(catalogFallbackVideo);
                }
                if (cancelled) return;
                const nextVideos = requested ? [...matchingDemoVideos, ...catalogFallbackVideos] : matchingDemoVideos;
                setVideos(nextVideos);
                setSelectedId(nextVideos[0]?.exercise_id || "");
            } catch (err) {
                if (!cancelled) setError((err as Error).message);
            }
        };
        void loadReferences();
        return () => {
            cancelled = true;
        };
    }, [requestedExerciseIds]);

    const selectedVideo = useMemo(
        () => videos.find((video) => video.exercise_id === selectedId) || videos[0],
        [videos, selectedId]
    );

    const completedIdentifiers = useMemo(
        () => rehabSession?.checklist.filter((item) => item.completed).map((item) => item.exercise_id || item.label) || [],
        [rehabSession]
    );

    const persistSession = async (completedItems: string[]) => {
        if (!rehabSession || sessionBusy) return;
        const auth = loadAuth();
        if (!auth || auth.role !== "patient") {
            setSessionError("Log in as a patient to update this rehab session.");
            return;
        }
        const submittedPatientNotes = patientNotesRef.current;
        setSessionBusy(true);
        setSessionError("");
        setSessionStatus("");
        try {
            const updated = await updateEpisodeRehabSession(
                rehabSession.session_id,
                {
                    completed_items: completedItems,
                    patient_notes: submittedPatientNotes,
                },
                auth.access_token
            );
            setRehabSession(updated);
            if (patientNotesRef.current === submittedPatientNotes) {
                updatePatientNotes(updated.patient_notes);
            }
            setSessionStatus("Progress saved.");
        } catch (err) {
            setSessionError((err as Error).message);
        } finally {
            setSessionBusy(false);
        }
    };

    const toggleChecklistItem = (identifier: string) => {
        const nextCompleted = completedIdentifiers.includes(identifier)
            ? completedIdentifiers.filter((item) => item !== identifier)
            : [...completedIdentifiers, identifier];
        void persistSession(nextCompleted);
    };

    const summarizeSession = async () => {
        if (!rehabSession || sessionBusy) return;
        const auth = loadAuth();
        if (!auth || auth.role !== "patient") {
            setSessionError("Log in as a patient to summarize this rehab session.");
            return;
        }
        const submittedPatientNotes = patientNotesRef.current;
        setSessionBusy(true);
        setSessionError("");
        setSessionStatus("");
        try {
            await updateEpisodeRehabSession(
                rehabSession.session_id,
                {
                    completed_items: completedIdentifiers,
                    patient_notes: submittedPatientNotes,
                },
                auth.access_token
            );
            const updated = await summarizeEpisodeRehabSession(rehabSession.session_id, auth.access_token);
            setRehabSession(updated);
            if (patientNotesRef.current === submittedPatientNotes) {
                updatePatientNotes(updated.patient_notes);
            }
            setSessionStatus("Summary saved to History.");
        } catch (err) {
            setSessionError((err as Error).message);
        } finally {
            setSessionBusy(false);
        }
    };

    useEffect(() => {
        if (!selectedId || selectedVideo?.catalogOnly) {
            setPoseDetail(null);
            return;
        }
        let cancelled = false;
        setPoseDetail(null);
        getAclPoseExtraction(selectedId)
            .then((data) => {
                if (!cancelled) setPoseDetail(data.extraction);
            })
            .catch(() => {
                if (!cancelled) setPoseDetail(null);
            });
        return () => {
            cancelled = true;
        };
    }, [selectedId, selectedVideo?.catalogOnly]);

    const stopSession = useCallback(() => {
        sessionAttemptRef.current += 1;
        detectionActiveRef.current = false;
        if (rafRef.current) cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
        const frameVideo = videoRef.current as VideoFrameVideo | null;
        if (frameVideo?.cancelVideoFrameCallback && videoFrameCallbackRef.current !== null) frameVideo.cancelVideoFrameCallback(videoFrameCallbackRef.current);
        videoFrameCallbackRef.current = null;
        if (wsRef.current && wsRef.current.readyState <= 1) wsRef.current.close();
        wsRef.current = null;
        if (videoRef.current?.srcObject) {
            const tracks = (videoRef.current.srcObject as MediaStream).getTracks();
            tracks.forEach((track) => track.stop());
            videoRef.current.srcObject = null;
        }
        const canvas = canvasRef.current;
        const ctx = canvas?.getContext("2d");
        if (canvas && ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
        lastVideoTimeRef.current = -1;
        lastDetectorTimestampRef.current = -1;
        setStatus("Ready");
        setLandmarkCount(0);
    }, []);

    useEffect(() => {
        return () => stopSession();
    }, [stopSession]);

    const poseFrames = poseDetail?.pose_data?.frames || [];
    const localReferenceUrl = mediaUrl(selectedVideo?.public_video_url);
    const onlineVideoUrl = localReferenceUrl || selectedVideo?.onlineVideoUrl || mediaUrl(selectedVideo?.video_url);
    const referenceHasPoseOverlay = Boolean(localReferenceUrl && poseFrames.length);

    useEffect(() => {
        if (sourceRafRef.current) cancelAnimationFrame(sourceRafRef.current);
        const canvas = sourceCanvasRef.current;
        const ctx = canvas?.getContext("2d");
        if (canvas && ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);

        const drawReferencePose = () => {
            const video = sourceVideoRef.current;
            const targetCanvas = sourceCanvasRef.current;
            if (!video || !targetCanvas || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
            const frame = nearestPoseFrame(poseFrames, video.currentTime);
            drawPose(targetCanvas, video, frame?.landmarks || [], "contain");
        };

        const tick = () => {
            drawReferencePose();
            sourceRafRef.current = requestAnimationFrame(tick);
        };

        const video = sourceVideoRef.current;
        const events = ["loadedmetadata", "loadeddata", "canplay", "seeked", "timeupdate", "play", "pause"];
        events.forEach((eventName) => video?.addEventListener(eventName, drawReferencePose));
        if (referenceHasPoseOverlay) sourceRafRef.current = requestAnimationFrame(tick);

        return () => {
            if (sourceRafRef.current) cancelAnimationFrame(sourceRafRef.current);
            sourceRafRef.current = null;
            events.forEach((eventName) => video?.removeEventListener(eventName, drawReferencePose));
        };
    }, [poseFrames, selectedId, localReferenceUrl, referenceHasPoseOverlay]);

    const loadPoseModel = async () => {
        if (poseLandmarkerRef.current) return;
        setModelStatus("Loading");
        const vision = await importVisionTasks(VISION_TASKS_URL);
        const filesetResolver = await vision.FilesetResolver.forVisionTasks(VISION_WASM_BASE);
        poseLandmarkerRef.current = await vision.PoseLandmarker.createFromOptions(filesetResolver, {
            baseOptions: {
                modelAssetPath: "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
            },
            runningMode: "VIDEO",
            numPoses: 1,
        });
        setModelStatus("Loaded");
    };

    const initWebcam = async () => {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
            audio: false,
        });
        if (videoRef.current) {
            videoRef.current.srcObject = stream;
            await videoRef.current.play();
        }
    };

    const extractAndSendLoop = useCallback(() => {
        if (rafRef.current) cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
        const frameVideo = videoRef.current as VideoFrameVideo | null;
        if (frameVideo?.cancelVideoFrameCallback && videoFrameCallbackRef.current !== null) frameVideo.cancelVideoFrameCallback(videoFrameCallbackRef.current);
        videoFrameCallbackRef.current = null;

        const processFrame = (mediaTimeSeconds?: number) => {
            const video = videoRef.current;
            const canvas = canvasRef.current;
            const poseLandmarker = poseLandmarkerRef.current;
            if (!detectionActiveRef.current || !video || !canvas || !poseLandmarker || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;

            const videoTime = mediaTimeSeconds ?? video.currentTime;
            if (videoTime === lastVideoTimeRef.current) return;
            lastVideoTimeRef.current = videoTime;
            const mediaTimestampMs = Math.round(videoTime * 1000);
            const timestampMs = Math.max(mediaTimestampMs, lastDetectorTimestampRef.current + 1);
            lastDetectorTimestampRef.current = timestampMs;
            const now = performance.now();

            try {
                const result = poseLandmarker.detectForVideo(video, timestampMs);
                const first = result.landmarks?.[0] || [];
                setLandmarkCount(first.length);
                drawPose(canvas, video, first, "cover");

                const ws = wsRef.current;
                if (first.length && ws?.readyState === WebSocket.OPEN && now - lastSentAtRef.current > 90) {
                    const coords = first.map((lm) => ({ x: lm.x, y: lm.y, z: lm.z ?? 0, visibility: lm.visibility ?? 1 }));
                    ws.send(JSON.stringify({ coordinates: coords }));
                    lastSentAtRef.current = now;
                }
            } catch (err) {
                setError((err as Error).message || "Pose detection failed.");
                setStatus("Detection error");
            }
        };

        const scheduleVideoFrame = () => {
            const video = videoRef.current as VideoFrameVideo | null;
            if (!video?.requestVideoFrameCallback) return false;
            videoFrameCallbackRef.current = video.requestVideoFrameCallback((_now, metadata) => {
                processFrame(metadata.mediaTime);
                if (detectionActiveRef.current) scheduleVideoFrame();
            });
            return true;
        };

        if (!scheduleVideoFrame()) {
            const tick = () => {
                processFrame();
                if (detectionActiveRef.current) rafRef.current = requestAnimationFrame(tick);
            };
            rafRef.current = requestAnimationFrame(tick);
        }
    }, []);

    const startSession = async () => {
        let attempt = 0;
        setError("");
        setScoringNotice("");
        setStatus("Starting");
        try {
            stopSession();
            attempt = sessionAttemptRef.current;
            setStatus("Starting");
            await initWebcam();
            if (attempt !== sessionAttemptRef.current) return;
            await loadPoseModel();
            if (attempt !== sessionAttemptRef.current) return;
            detectionActiveRef.current = true;
            setStatus("Live");
            extractAndSendLoop();

            const auth = loadAuth();
            if (!auth || auth.role !== "patient" || !auth.access_token) {
                setScoringNotice("Log in for live scoring. Tracking still works on this device.");
                return;
            }

            if (!referenceHasPoseOverlay) {
                setScoringNotice("Scoring isn't ready for this exercise yet. Tracking still works on this device.");
                return;
            }

            let ticket = "";
            try {
                ticket = await issuePatientStreamTicket(auth.access_token);
            } catch {
                if (attempt !== sessionAttemptRef.current) return;
                setScoringNotice("Scoring is paused for now. Tracking still works on this device.");
                return;
            }
            if (attempt !== sessionAttemptRef.current) return;
            if (!ticket) return;

            try {
                const ws = connectPatientStream(ticket);
                wsRef.current = ws;
                ws.onopen = () => {
                    if (wsRef.current !== ws) return;
                    setError("");
                };
                ws.onmessage = (event) => {
                    if (wsRef.current !== ws) return;
                    try {
                        const data = JSON.parse(event.data) as PatientScorePayload;
                        if (typeof data.current_score === "number") setScore(data.current_score);
                    } catch {
                        // Non-JSON messages are ignored by the prototype stream.
                    }
                };
                ws.onerror = () => {
                    if (wsRef.current !== ws) return;
                    setScoringNotice("Scoring is paused for now. Tracking still works on this device.");
                };
                ws.onclose = () => {
                    if (wsRef.current !== ws || !detectionActiveRef.current) return;
                    setScoringNotice("Scoring is paused for now. Tracking still works on this device.");
                };
            } catch {
                if (attempt !== sessionAttemptRef.current) return;
                setScoringNotice("Scoring is paused for now. Tracking still works on this device.");
                return;
            }
        } catch (err) {
            if (attempt !== sessionAttemptRef.current) return;
            setStatus("Ready");
            setError((err as Error).message);
        }
    };

    const tracking = trackingState(status, landmarkCount, modelStatus);
    const friendlyErrorText = error ? friendlyError(error) : "";
    const checklistDone = rehabSession?.checklist.filter((item) => item.completed).length ?? 0;
    const checklistTotal = rehabSession?.checklist.length ?? 0;
    const isDemo = !patientId || !episodeId;
    const sessionRunning = status === "Live" || status === "Starting";

    return (
        <div className="space-y-8">
            <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4" data-tone="mint">
                <div>
                    <p className="page-header-kicker">AI Daily Rehab</p>
                    <h1 className="font-hand text-[clamp(2.25rem,5vw,3.5rem)] leading-[0.95] tracking-[-0.02em]">Today&apos;s session</h1>
                </div>
                {isDemo ? (
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-2 rounded-full bg-fill px-4 py-2 text-sm font-semibold text-ink">
                            <span aria-hidden="true" className="h-2 w-2 rounded-full bg-[var(--rf-ink-soft)]" />
                            {patientId ? "Demo, not saved to an episode" : "Demo session"}
                        </span>
                        {patientId ? null : (
                            <a href="/login" className="rounded-full px-3 py-2 text-sm font-semibold text-ink underline decoration-[var(--rf-line-strong)] underline-offset-4 hover:decoration-ink">
                                Log in to save progress
                            </a>
                        )}
                    </div>
                ) : (
                    <span className="inline-flex items-center gap-2 rounded-full bg-mint px-4 py-2 text-sm font-semibold text-mint-deep">
                        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-mint-stroke" />
                        Saving to this Care Episode
                    </span>
                )}
            </header>

            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px] lg:items-start">
                <div className="min-w-0 space-y-4">
                    <section aria-label="Live camera feed" className="relative overflow-hidden rounded-[32px] bg-[#111111] shadow-lift">
                        <video ref={videoRef} className="aspect-[4/5] w-full scale-x-[-1] object-cover sm:aspect-video" playsInline muted />
                        <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full scale-x-[-1]" />
                        {status === "Ready" ? (
                            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center">
                                <span aria-hidden="true" className="flex h-16 w-16 items-center justify-center rounded-full bg-white/10 text-white">
                                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                                        <path d="M15 10.5 20 7v10l-5-3.5" />
                                        <rect x="3" y="6" width="12" height="12" rx="3" />
                                    </svg>
                                </span>
                                <p className="font-hand text-3xl text-white sm:text-4xl">{gateBlocked ? "Safety check first" : "Ready when you are"}</p>
                                <p className="text-sm text-white/60">Video stays on this device.</p>
                            </div>
                        ) : null}
                        <div className="absolute inset-x-3 top-3 flex flex-wrap items-start justify-between gap-2 sm:inset-x-5 sm:top-5">
                            <span className="inline-flex items-center gap-2 rounded-full bg-white/95 px-3.5 py-1.5 text-sm font-semibold text-ink">
                                <span aria-hidden="true" className={`session-dot ${tracking.dot}`} />
                                {tracking.label}
                            </span>
                            {selectedVideo ? (
                                <span className="tag max-w-full truncate bg-mint-label py-1.5 text-mint-deep sm:max-w-[60%]" data-tone="mint">
                                    <span className="truncate">{selectedVideo.title}</span>
                                </span>
                            ) : null}
                        </div>
                        {status === "Live" ? (
                            <div className="sticker tone-mint tilt-l absolute bottom-4 left-4 flex items-end gap-3 px-5 py-3 sm:bottom-6 sm:left-6">
                                <span className="font-hand text-6xl leading-[0.85] text-ink">{score}</span>
                                <span className="pb-1 text-sm font-semibold leading-tight text-mint-deep">
                                    Form
                                    <br />
                                    score
                                </span>
                            </div>
                        ) : null}
                    </section>

                    {gateBlocked ? (
                        <div className="sticker tone-peach flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                            <div>
                                <p className="font-hand text-3xl leading-none text-ink">Triage comes first</p>
                                <p className="mt-2 text-sm text-peach-deep">This episode needs AI Triage Intake before guided exercise.</p>
                            </div>
                            <a className="btn-primary shrink-0" href={`/episodes/${episodeId}/triage`}>
                                Open AI Triage Intake
                                <span aria-hidden="true" className="btn-arrow">
                                    <ArrowIcon />
                                </span>
                            </a>
                        </div>
                    ) : (
                        <div className="flex flex-wrap items-center gap-3">
                            <button className={`${sessionRunning ? "btn-secondary" : "btn-primary"} min-w-[9rem] pl-6 pr-3`} onClick={startSession} disabled={status === "Starting" || status === "Live"}>
                                Start
                                <span aria-hidden="true" className="btn-arrow">
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                                        <path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5Z" />
                                    </svg>
                                </span>
                            </button>
                            <button className={`${sessionRunning ? "btn-primary" : "btn-secondary"} min-w-[7rem]`} onClick={stopSession} disabled={status !== "Live" && status !== "Starting"}>
                                Stop
                            </button>
                        </div>
                    )}

                    {friendlyErrorText ? (
                        <p className="notice notice-danger flex items-start gap-3" role="alert">
                            <span aria-hidden="true" className="mt-[0.45em] h-2 w-2 shrink-0 rounded-full bg-rose-600" />
                            {friendlyErrorText}
                        </p>
                    ) : null}
                    {scoringNotice ? <p className="notice text-[var(--rf-ink-muted)]">{scoringNotice}</p> : null}
                </div>

                <aside className="min-w-0 space-y-4">
                    <section aria-label="Reference movement video" className="card p-5">
                        <h2 className="font-hand text-3xl leading-none">How it&apos;s done</h2>
                        <div className="mt-4">
                            {localReferenceUrl ? (
                                <div className="relative overflow-hidden rounded-[22px] bg-[#111111]">
                                    <video key={selectedVideo?.exercise_id} ref={sourceVideoRef} className="aspect-video w-full object-contain" src={localReferenceUrl} controls playsInline />
                                    <canvas ref={sourceCanvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />
                                </div>
                            ) : onlineVideoUrl ? (
                                <div className="overflow-hidden rounded-[22px] bg-[#111111]">
                                    <iframe key={selectedVideo?.exercise_id} className="aspect-video w-full" src={onlineVideoUrl} title={selectedVideo?.title || "Reference video"} allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowFullScreen />
                                </div>
                            ) : (
                                <div className="flex aspect-video flex-col items-center justify-center rounded-[22px] bg-fill px-4 text-center">
                                    <p className="font-hand text-2xl text-[var(--rf-ink-soft)]">No demo video yet</p>
                                </div>
                            )}
                        </div>
                        {selectedVideo ? <p className="mt-4 font-semibold leading-snug text-ink">{selectedVideo.title}</p> : null}
                        {selectedVideo?.source_url ? (
                            <a className="mt-2 inline-flex items-center gap-1.5 rounded-full text-sm font-semibold text-[var(--rf-ink-muted)] underline decoration-[var(--rf-line-strong)] underline-offset-4 hover:text-ink hover:decoration-ink" href={selectedVideo.source_url} target="_blank" rel="noreferrer">
                                Original exercise page
                                <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M7 17 17 7M9 7h8v8" />
                                </svg>
                            </a>
                        ) : null}
                    </section>

                    <div className="flex items-start gap-3 rounded-[22px] bg-rose-100 px-4 py-3.5 text-sm text-rose-700">
                        <svg aria-hidden="true" className="mt-0.5 shrink-0" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <circle cx="12" cy="12" r="9" />
                            <path d="M12 7.5v5.5M12 16.5h.01" />
                        </svg>
                        <p>
                            <span className="font-semibold">Stop if pain turns sharp or keeps building.</span> Rest, and tell your clinician before the next session.
                        </p>
                    </div>
                </aside>
            </div>

            {hasRealSessionContext && rehabSession ? (
                <section className="card sm:p-8">
                    <div className="flex flex-wrap items-end justify-between gap-4">
                        <h2 className="font-hand text-4xl leading-none">Checklist</h2>
                        <p className="flex items-baseline gap-2" aria-label={`${checklistDone} of ${checklistTotal} done`}>
                            <span className="font-hand text-5xl leading-none text-ink">
                                {checklistDone}
                                <span className="text-[var(--rf-ink-soft)]">/{checklistTotal}</span>
                            </span>
                            <span className="text-sm font-semibold text-[var(--rf-ink-muted)]">done</span>
                        </p>
                    </div>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2">
                        {rehabSession.checklist.map((item, index) => {
                            const identifier = item.exercise_id || item.label;
                            return (
                                <label
                                    key={item.exercise_id || item.label + "-" + index}
                                    className={`flex min-h-[64px] cursor-pointer items-center gap-4 rounded-[22px] px-4 py-3 transition-colors ${item.completed ? "bg-mint" : "bg-fill hover:bg-[var(--rf-fill-strong)]"} ${sessionBusy ? "cursor-wait opacity-70" : ""}`}
                                >
                                    <input className="peer sr-only" type="checkbox" checked={item.completed} onChange={() => toggleChecklistItem(identifier)} disabled={sessionBusy} />
                                    <span
                                        aria-hidden="true"
                                        className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full border-2 transition-colors peer-focus-visible:outline peer-focus-visible:outline-[3px] peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ink ${item.completed ? "border-mint-stroke bg-mint-stroke text-white" : "border-[var(--rf-line-strong)] bg-white text-transparent"}`}
                                    >
                                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                                            <path d="m5 12.5 4.5 4.5L19 7.5" />
                                        </svg>
                                    </span>
                                    <span className={`font-semibold leading-snug ${item.completed ? "text-mint-deep" : "text-ink"}`}>{item.label}</span>
                                </label>
                            );
                        })}
                    </div>
                    <label className="mt-6 block">
                        <span className="field-label mt-0">Your notes</span>
                        <textarea className="textarea mt-2 min-h-24" value={patientNotes} onChange={(event) => updatePatientNotes(event.target.value)} placeholder="Symptoms before or after today's session" />
                    </label>
                    {rehabSession.session_summary ? (
                        <div className="mt-5 rounded-[22px] bg-mint px-5 py-4">
                            <p className="text-sm font-semibold text-mint-deep">Summary</p>
                            <p className="mt-1 text-ink">{rehabSession.session_summary}</p>
                        </div>
                    ) : null}
                    <div className="mt-5 flex flex-wrap items-center gap-3">
                        <button className="btn bg-mint-label text-mint-deep hover:bg-mint" onClick={summarizeSession} disabled={sessionBusy}>
                            Finish and summarise
                        </button>
                        <button className="btn-secondary" onClick={() => void persistSession(completedIdentifiers)} disabled={sessionBusy}>
                            {sessionBusy ? "Saving" : "Save progress"}
                        </button>
                        {sessionStatus ? (
                            <p className="inline-flex items-center gap-2 text-sm font-semibold text-mint-deep">
                                <span aria-hidden="true" className="h-2 w-2 rounded-full bg-mint-stroke" />
                                {sessionStatus}
                            </p>
                        ) : null}
                    </div>
                    {sessionError ? <p className="notice notice-danger mt-4">{sessionError}</p> : null}
                </section>
            ) : hasRealSessionContext ? (
                <section className="card text-center">
                    {sessionLoading ? (
                        <p className="font-hand text-2xl text-[var(--rf-ink-soft)]">Loading your checklist…</p>
                    ) : (
                        <p className="notice notice-danger text-left">{sessionError || "This rehab session is unavailable."}</p>
                    )}
                </section>
            ) : null}

            <section aria-labelledby="session-exercises-title">
                <div className="flex flex-wrap items-baseline gap-3">
                    <h2 id="session-exercises-title" className="font-hand text-4xl leading-none">
                        Exercises
                    </h2>
                    {videos.length ? (
                        <span className="tag" data-tone="mint">
                            {videos.length}
                        </span>
                    ) : null}
                </div>
                {videos.length ? (
                    <div className="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                        {videos.map((video, index) => {
                            const isSelected = selectedId === video.exercise_id;
                            return (
                                <button
                                    key={video.exercise_id}
                                    onClick={() => setSelectedId(video.exercise_id)}
                                    aria-pressed={isSelected}
                                    className={`sticker flex w-full items-start gap-4 p-5 text-left ${isSelected ? "tone-mint rotate-[-1.2deg]" : "bg-fill shadow-none"}`}
                                >
                                    <span aria-hidden="true" className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full font-hand text-2xl ${isSelected ? "bg-white text-mint-deep" : "bg-white text-[var(--rf-ink-muted)]"}`}>
                                        {index + 1}
                                    </span>
                                    <span className="min-w-0">
                                        <span className="block font-semibold leading-snug text-ink">{video.title}</span>
                                        {video.relevance_notes ? <span className="mt-1.5 line-clamp-2 block text-sm text-[var(--rf-ink-muted)]">{video.relevance_notes}</span> : null}
                                        {isSelected ? (
                                            <span className="tag mt-3 bg-white text-mint-deep">
                                                <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-mint-stroke" />
                                                On screen
                                            </span>
                                        ) : null}
                                    </span>
                                </button>
                            );
                        })}
                    </div>
                ) : (
                    <div className="mt-5 flex flex-col items-center justify-center gap-1 rounded-[28px] border-2 border-dashed border-[var(--rf-line-strong)] px-6 py-12 text-center">
                        <p className="font-hand text-3xl text-ink">No exercises here yet</p>
                    </div>
                )}
            </section>
        </div>
    );
}

function ArrowIcon() {
    return (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 12h14M13 6l6 6-6 6" />
        </svg>
    );
}
