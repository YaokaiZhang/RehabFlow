"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
    getAclPoseExtraction,
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
            setSessionStatus("Session progress saved.");
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
            setSessionStatus("Session summary saved to Episode Memory inputs.");
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
                setScoringNotice("Sign in for live scoring and saved progress. Pose detection continues locally.");
                return;
            }

            if (!referenceHasPoseOverlay) {
                setScoringNotice("Scoring will be available when reference pose data is added for this movement.");
                return;
            }

            let ticket = "";
            try {
                ticket = await issuePatientStreamTicket(auth.access_token);
            } catch {
                if (attempt !== sessionAttemptRef.current) return;
                setScoringNotice("Scoring is temporarily unavailable. Pose detection continues locally.");
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
                    setScoringNotice("Scoring is temporarily unavailable. Pose detection continues locally.");
                };
                ws.onclose = () => {
                    if (wsRef.current !== ws || !detectionActiveRef.current) return;
                    setScoringNotice("Scoring is temporarily unavailable. Pose detection continues locally.");
                };
            } catch {
                if (attempt !== sessionAttemptRef.current) return;
                setScoringNotice("Scoring is temporarily unavailable. Pose detection continues locally.");
                return;
            }
        } catch (err) {
            if (attempt !== sessionAttemptRef.current) return;
            setStatus("Ready");
            setError((err as Error).message);
        }
    };

    return (
        <div className="space-y-5">
            <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
                <div className="grid lg:grid-cols-[minmax(0,1fr)_360px]">
                    <div className="flex flex-col gap-4 p-4">
                        <section aria-label="Live camera feed" className="relative overflow-hidden rounded-md bg-slate-950">
                            <video ref={videoRef} className="aspect-video w-full scale-x-[-1] object-cover" playsInline muted />
                            <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full scale-x-[-1]" />
                            <div className="absolute left-4 top-4 flex flex-wrap gap-2">
                                <span className="rounded bg-white/90 px-3 py-1 text-xs font-semibold text-slate-900">{status}</span>
                                <span className="rounded bg-teal-400 px-3 py-1 text-xs font-semibold text-slate-950">{landmarkCount} landmarks</span>
                            </div>
                        </section>
                        <section aria-label="Reference movement video" className="relative flex flex-col rounded-md border border-slate-200 bg-white p-4">
                            <div>
                                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Reference movement</p>
                                <h2 className="mt-1 text-xl font-semibold text-slate-950">{selectedVideo?.title || "Reference"}</h2>
                                <p className="mt-1 text-xs text-slate-500">{referenceHasPoseOverlay ? "Pose overlay appears when downloaded pose data is available." : "Reference video only. Pose data for this movement can be added later."}</p>
                            </div>
                            <div className="mt-4">
                                {localReferenceUrl ? (
                                    <div className="relative overflow-hidden rounded-md bg-slate-950">
                                        <video key={selectedVideo?.exercise_id} ref={sourceVideoRef} className="aspect-video w-full object-contain" src={localReferenceUrl} controls playsInline />
                                        <canvas ref={sourceCanvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />
                                    </div>
                                ) : onlineVideoUrl ? (
                                    <div className="overflow-hidden rounded-md bg-slate-950">
                                        <iframe key={selectedVideo?.exercise_id} className="aspect-video w-full" src={onlineVideoUrl} title={selectedVideo?.title || "Reference video"} allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowFullScreen />
                                    </div>
                                ) : (
                                    <div className="flex aspect-video items-center justify-center rounded-md bg-slate-100 text-sm text-slate-500">No media available</div>
                                )}
                            </div>
                            <a className="btn-secondary mt-4 w-full" href={selectedVideo?.source_url || "#"} target="_blank" rel="noreferrer">
                                Source
                            </a>
                        </section>
                    </div>
                    <aside className="flex flex-col gap-4 border-t border-slate-200 p-5 lg:border-l lg:border-t-0">
                        <div>
                            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Self-guided rehab</p>
                            <h1 className="mt-1 text-2xl font-semibold text-slate-950">Live Rehab Session</h1>
                            <p className="mt-1 text-xs text-slate-500">Today&apos;s guided movement session</p>
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                            <div className="rounded-md border border-slate-200 p-3">
                                <p className="text-xs text-slate-500">Score</p>
                                <p className="text-3xl font-semibold text-emerald-700">{score}</p>
                            </div>
                            <div className="rounded-md border border-slate-200 p-3">
                                <p className="text-xs text-slate-500">Model</p>
                                <p className="text-lg font-semibold text-slate-900">{modelStatus}</p>
                            </div>
                        </div>
                        <div className="flex gap-2">
                            <button className="btn-primary flex-1" onClick={startSession} disabled={status === "Starting" || status === "Live"}>
                                Start
                            </button>
                            <button className="btn-secondary" onClick={stopSession}>
                                Stop
                            </button>
                        </div>
                        <div className="rounded-md bg-slate-50 p-3 text-sm text-slate-600">
                            <p className="font-semibold text-slate-900">Session context</p>
                            <p className="mt-1">{patientId ? "Signed in and ready to save progress." : "Sign in to save progress."}</p>
                            {episodeId ? <p className="mt-1">Connected to this Care Episode.</p> : null}
                        </div>
                        {scoringNotice ? <p className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">{scoringNotice}</p> : null}
                        {error ? <p className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</p> : null}
                    </aside>
                </div>
            </section>

            {hasRealSessionContext && rehabSession ? (
                <section className="card max-w-4xl border-emerald-200 bg-emerald-50">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                            <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Active rehab session</p>
                            <h2 className="section-title mt-1">Session checklist</h2>
                        </div>
                        <button className="btn-secondary" onClick={() => void persistSession(completedIdentifiers)} disabled={sessionBusy}>
                            {sessionBusy ? "Saving" : "Save Session Progress"}
                        </button>
                    </div>
                    <div className="mt-4 space-y-2">
                        {rehabSession.checklist.map((item, index) => {
                            const identifier = item.exercise_id || item.label;
                            return (
                                <label key={item.exercise_id || item.label + "-" + index} className="flex items-center gap-3 rounded-md border border-emerald-100 bg-white p-3 text-sm text-slate-700">
                                    <input type="checkbox" checked={item.completed} onChange={() => toggleChecklistItem(identifier)} disabled={sessionBusy} />
                                    <span className="font-medium text-slate-950">{item.label}</span>
                                </label>
                            );
                        })}
                    </div>
                    <label className="mt-4 block">
                        <span className="field-label">Patient notes</span>
                        <textarea className="textarea min-h-24" value={patientNotes} onChange={(event) => updatePatientNotes(event.target.value)} placeholder="Symptoms before or after today's session" />
                    </label>
                    {rehabSession.session_summary ? <p className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">{rehabSession.session_summary}</p> : null}
                    <button className="btn-primary mt-4" onClick={summarizeSession} disabled={sessionBusy}>Generate Session Summary</button>
                    {sessionError ? <p className="mt-3 text-sm text-rose-700">{sessionError}</p> : null}
                    {sessionStatus ? <p className="mt-3 text-sm text-emerald-700">{sessionStatus}</p> : null}
                </section>
            ) : hasRealSessionContext ? (
                <section className="card max-w-4xl text-sm text-slate-600">
                    {sessionLoading ? "Loading session details..." : sessionError || "This rehab session is unavailable."}
                </section>
            ) : null}

            <section className="card">
                <div>
                    <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Exercise selection</p>
                    <h2 className="section-title mt-1">Choose a reference movement</h2>
                    <p className="mt-1 text-sm text-slate-600">Select an exercise to update the reference video beside your patient feed.</p>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                    {videos.map((video) => (
                        <button
                            key={video.exercise_id}
                            onClick={() => setSelectedId(video.exercise_id)}
                            className={selectedId === video.exercise_id ? "w-full rounded-md border border-teal-500 bg-teal-50 p-4 text-left transition" : "w-full rounded-md border border-slate-200 bg-white p-4 text-left transition hover:border-slate-300"}
                        >
                            <p className="font-semibold text-slate-950">{video.title}</p>
                            <p className="mt-2 line-clamp-2 text-sm text-slate-600">{video.relevance_notes}</p>
                            <p className="mt-2 text-xs font-medium text-slate-500">{video.public_video_url ? "Downloaded reference" : "Online reference"}</p>
                        </button>
                    ))}
                    {!videos.length ? <p className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-600">No exercise videos are attached to this session yet.</p> : null}
                </div>
            </section>
        </div>
    );
}
