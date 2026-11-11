/*
 * Página de verificación: cámara frontal, grabación y subida del video.
 *
 * Solo graba en vivo con la cámara. No hay ningún campo para elegir archivos
 * de la galería. El resultado de la verificación se decide en el servidor.
 */
(function () {
  "use strict";

  const config = window.VERIFY_CONFIG;

  // Formatos a probar, en orden de preferencia. Chrome/Android graba WebM y
  // Safari/iOS graba MP4.
  const MIME_CANDIDATES = [
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/webm",
    "video/mp4;codecs=avc1",
    "video/mp4",
  ];

  const steps = {
    intro: document.getElementById("step-intro"),
    camera: document.getElementById("step-camera"),
    uploading: document.getElementById("step-uploading"),
    done: document.getElementById("step-done"),
    error: document.getElementById("step-error"),
  };
  const el = {
    btnStart: document.getElementById("btn-start"),
    btnRecord: document.getElementById("btn-record"),
    btnRetry: document.getElementById("btn-retry"),
    preview: document.getElementById("preview"),
    recIndicator: document.getElementById("rec-indicator"),
    recText: document.getElementById("rec-text"),
    cameraHint: document.getElementById("camera-hint"),
    progressBar: document.getElementById("progress-bar"),
    progressText: document.getElementById("progress-text"),
    errorTitle: document.getElementById("error-title"),
    errorMessage: document.getElementById("error-message"),
  };

  let stream = null;
  let recordedBlob = null;
  let retryAction = null;

  // ---------- Utilidades de interfaz ----------

  function showStep(name) {
    Object.entries(steps).forEach(([key, section]) => { section.hidden = key !== name; });
    window.scrollTo(0, 0);
  }

  function showError(title, message, retry) {
    stopCamera();
    el.errorTitle.textContent = title;
    el.errorMessage.textContent = message;
    retryAction = retry ? retry.action : null;
    el.btnRetry.hidden = !retry;
    if (retry) el.btnRetry.textContent = retry.label;
    showStep("error");
  }

  // ---------- Cámara ----------

  function pickMimeType() {
    for (const type of MIME_CANDIDATES) {
      if (MediaRecorder.isTypeSupported(type)) return type;
    }
    return ""; // El navegador elige su formato por defecto.
  }

  function extensionFor(mimeType) {
    return mimeType.includes("mp4") ? "mp4" : "webm";
  }

  function stopCamera() {
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
  }

  async function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError(
        "No se puede usar la cámara",
        "Tu navegador no permite usar la cámara en esta página. Abrí el link desde Chrome o Safari.",
        null,
      );
      return;
    }
    if (typeof MediaRecorder === "undefined") {
      showError(
        "No se puede grabar video",
        "Tu navegador no permite grabar video. Actualizalo o abrí el link desde Chrome o Safari.",
        null,
      );
      return;
    }

    el.btnStart.disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 720 }, height: { ideal: 960 } },
        audio: false,
      });
    } catch (err) {
      el.btnStart.disabled = false;
      handleCameraError(err);
      return;
    }

    el.preview.srcObject = stream;
    el.btnRecord.disabled = true;
    el.btnRecord.hidden = false;
    el.recIndicator.hidden = true;
    el.cameraHint.textContent = "Ubicá tu cara dentro del óvalo.";
    showStep("camera");
    el.preview.onloadedmetadata = () => { el.btnRecord.disabled = false; };
  }

  function handleCameraError(err) {
    console.error("getUserMedia:", err);
    const retry = { label: "Intentar de nuevo", action: startCamera };
    switch (err && err.name) {
      case "NotAllowedError":
      case "SecurityError":
        showError(
          "Permiso de cámara denegado",
          "Para continuar necesitamos usar tu cámara. Habilitá el permiso en la configuración del navegador y volvé a intentar.",
          retry,
        );
        break;
      case "NotFoundError":
      case "OverconstrainedError":
        showError("No encontramos una cámara", "Tu dispositivo no tiene una cámara frontal disponible.", null);
        break;
      case "NotReadableError":
        showError(
          "La cámara está en uso",
          "Otra aplicación está usando la cámara. Cerrala y volvé a intentar.",
          retry,
        );
        break;
      default:
        showError("No se pudo abrir la cámara", "Ocurrió un error al abrir la cámara. Volvé a intentar.", retry);
    }
  }

  // ---------- Grabación ----------

  function startRecording() {
    const mimeType = pickMimeType();
    let recorder;
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch (err) {
      console.error("MediaRecorder:", err);
      showError("No se puede grabar video", "Tu navegador no permite grabar video.", null);
      return;
    }

    const chunks = [];
    recorder.ondataavailable = (event) => { if (event.data && event.data.size > 0) chunks.push(event.data); };
    recorder.onstop = () => {
      stopCamera();
      const type = recorder.mimeType || mimeType || "video/webm";
      recordedBlob = new Blob(chunks, { type });
      if (recordedBlob.size === 0) {
        showError("No se grabó el video", "La grabación quedó vacía. Volvé a intentar.", {
          label: "Grabar de nuevo", action: startCamera,
        });
        return;
      }
      if (recordedBlob.size > config.maxVideoBytes) {
        showError("El video es demasiado grande", "Volvé a grabar el video.", {
          label: "Grabar de nuevo", action: startCamera,
        });
        return;
      }
      upload();
    };

    el.btnRecord.hidden = true;
    el.recIndicator.hidden = false;
    el.cameraHint.textContent = "Mantené tu cara dentro del óvalo y mirá a la cámara.";

    let remaining = config.recordingSeconds;
    el.recText.textContent = `Grabando... ${remaining}`;
    recorder.start(250);

    const timer = setInterval(() => {
      remaining -= 1;
      if (remaining > 0) {
        el.recText.textContent = `Grabando... ${remaining}`;
        return;
      }
      clearInterval(timer);
      if (recorder.state !== "inactive") recorder.stop();
    }, 1000);
  }

  // ---------- Subida ----------

  function setProgress(percent) {
    el.progressBar.style.width = `${percent}%`;
    el.progressText.textContent = `${percent}%`;
    el.progressBar.parentElement.setAttribute("aria-valuenow", String(percent));
  }

  function readDetail(xhr) {
    try { return JSON.parse(xhr.responseText).detail; } catch (_) { return null; }
  }

  function upload() {
    setProgress(0);
    showStep("uploading");

    const form = new FormData();
    form.append("video", recordedBlob, `video.${extensionFor(recordedBlob.type)}`);

    // XMLHttpRequest en lugar de fetch para poder mostrar el progreso.
    const xhr = new XMLHttpRequest();
    xhr.open("POST", config.uploadUrl);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) setProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        recordedBlob = null;
        showStep("done");
        return;
      }
      handleUploadError(xhr.status, readDetail(xhr));
    };
    xhr.onerror = () => handleUploadError(0, null);
    xhr.send(form);
  }

  function handleUploadError(status, detail) {
    console.error("Subida fallida:", status, detail);
    if (status === 404 || status === 410) {
      showError("El link ya no es válido", detail || "El link expiró o ya fue utilizado.", null);
    } else if (status === 413 || status === 415 || status === 422) {
      showError("No pudimos procesar el video", detail || "Volvé a grabar el video.", {
        label: "Grabar de nuevo", action: startCamera,
      });
    } else {
      showError(
        "No pudimos enviar el video",
        "Revisá tu conexión a internet y volvé a intentar.",
        { label: "Reintentar envío", action: upload },
      );
    }
  }

  // ---------- Eventos ----------

  el.btnStart.addEventListener("click", startCamera);
  el.btnRecord.addEventListener("click", () => {
    el.btnRecord.disabled = true;
    startRecording();
  });
  el.btnRetry.addEventListener("click", () => { if (retryAction) retryAction(); });
  window.addEventListener("pagehide", stopCamera);
})();
