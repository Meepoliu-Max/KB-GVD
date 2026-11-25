(() => {
  window.lucide?.createIcons();

  const bytesPerMb = 1024 * 1024;
  const allowedExtensions = new Set(['pdf', 'docx', 'md', 'txt', 'markdown']);
  const uploadZone = document.getElementById('task-upload');
  const configuredLimit = Number(uploadZone.dataset.maxFileMb);
  const maxFileMb = Number.isFinite(configuredLimit) ? configuredLimit : 15;
  const uploadText = uploadZone.querySelector('.kb-upload-text');
  const uploadHint = document.getElementById('task-upload-hint');
  const nameInput = document.getElementById('task-name');
  const button = document.querySelector('[data-dom-id="btn-confirm-new-task"]');
  const cancelButton = document.querySelector('[data-dom-id="btn-cancel-new-task"]');
  const replaceButton = document.getElementById('replace-document');
  const fileError = document.getElementById('err-file');
  const nameError = document.getElementById('err-name');
  const sizeNotice = document.getElementById('upload-warning');
  const progress = document.getElementById('upload-progress');
  const reviewNotice = document.getElementById('document-review');
  const formError = document.getElementById('document-error');
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.pdf,.docx,.md,.markdown,.txt';
  fileInput.hidden = true;
  uploadZone.after(fileInput);
  uploadHint.textContent += `，单文件上限 ${maxFileMb} MB`;

  const params = new URLSearchParams(window.location.search);
  let fileId = params.get('file_id');
  let selectedFile = null;
  let busy = false;
  let review = '';
  let replaceRequired = false;

  function show(element, message = '') {
    element.textContent = message;
    element.hidden = !message;
  }

  function validateFile() {
    if (!selectedFile) return fileId ? '' : '请先选择要上传的文件';
    const extension = selectedFile.name.split('.').pop().toLowerCase();
    if (!selectedFile.name.includes('.') || !allowedExtensions.has(extension)) {
      return '不支持此文件格式。请上传 PDF、DOCX、Markdown 或 TXT。';
    }
    if (selectedFile.size > maxFileMb * bytesPerMb) {
      const sizeMb = (selectedFile.size / bytesPerMb).toFixed(2);
      const size = Number(sizeMb) > maxFileMb ? `${sizeMb} MB` : `${selectedFile.size.toLocaleString()} 字节`;
      return `此文件为 ${size}，单文件上限为 ${maxFileMb} MB。请按章节拆分或重新导出较小的文件。`;
    }
    return '';
  }

  function updateForm() {
    const issue = validateFile();
    fileError.textContent = selectedFile ? issue : '';
    nameError.textContent = selectedFile && !nameInput.value.trim() ? '请输入任务名称' : '';
    const largeFile = selectedFile && selectedFile.size > 10 * bytesPerMb;
    show(sizeNotice, !issue && largeFile ? '文件较大，上传和解析可能需要更长时间。' : '');
    show(reviewNotice, review);
    button.textContent = review ? '仍要处理' : '确认创建';
    button.disabled = busy || replaceRequired || !!issue || !nameInput.value.trim();
    cancelButton.disabled = busy;
    replaceButton.disabled = busy;
    replaceButton.hidden = !review && !replaceRequired;
    nameInput.disabled = busy;
    uploadZone.setAttribute('aria-disabled', String(busy));
  }

  function setBusy(message = '') {
    busy = !!message;
    show(progress, message);
    updateForm();
  }

  function rememberUpload() {
    const query = new URLSearchParams({ file_id: fileId, name: nameInput.value.trim() });
    history.replaceState(null, '', `/task-new?${query}`);
  }

  function openTask(taskId, status) {
    const page = status === 'completed' ? 'task-completed' : 'task-detail';
    const query = new URLSearchParams({ task_id: taskId, name: nameInput.value.trim() });
    window.location.replace(`/${page}?${query}`);
  }

  function chooseFile() {
    if (busy) return;
    fileInput.value = '';
    fileInput.click();
  }

  async function discardPendingUpload() {
    if (fileId && !replaceRequired) await KBRefiner.API.cancelTask(fileId);
    fileId = null;
    history.replaceState(null, '', '/task-new');
  }

  async function selectFile(file) {
    if (!file || busy) return;
    try {
      if (fileId) {
        setBusy('正在更换文件…');
        await discardPendingUpload();
      }
      const oldName = selectedFile?.name || uploadText.textContent;
      if (!nameInput.value.trim() || nameInput.value === oldName.replace(/\.[^.]+$/, '')) {
        nameInput.value = file.name.replace(/\.[^.]+$/, '');
      }
      selectedFile = file;
      uploadText.textContent = file.name;
      uploadHint.textContent = `${KBRefiner.formatBytes(file.size)} · 已选择`;
      review = '';
      replaceRequired = false;
      show(formError);
    } catch (error) {
      show(formError, error.message);
    } finally {
      setBusy();
    }
  }

  uploadZone.addEventListener('click', chooseFile);
  uploadZone.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    chooseFile();
  });
  uploadZone.addEventListener('dragover', event => event.preventDefault());
  uploadZone.addEventListener('drop', event => {
    event.preventDefault();
    selectFile(event.dataTransfer.files[0]);
  });
  replaceButton.addEventListener('click', chooseFile);
  fileInput.addEventListener('change', () => selectFile(fileInput.files[0]));
  nameInput.addEventListener('input', updateForm);

  button.addEventListener('click', async () => {
    if (button.disabled) return;
    show(formError);
    try {
      if (!fileId) {
        setBusy('正在上传文件…');
        const uploaded = await KBRefiner.API.upload(selectedFile);
        fileId = uploaded.file_id;
        rememberUpload();
      }
      setBusy('正在提取文字并检查文档…');
      const task = await KBRefiner.API.processAsync(fileId, {
        filename: nameInput.value.trim(),
        acceptDocumentWarnings: !!review,
      });
      openTask(task.task_id, task.status);
      return;
    } catch (error) {
      if (error.code === 'document_review_required') {
        review = error.message;
      } else {
        if (error.status === 422 && fileId) {
          review = '';
          replaceRequired = true;
        }
        show(formError, error.message);
      }
    }
    setBusy();
    if (review && !replaceRequired) button.focus();
    if (replaceRequired) replaceButton.focus();
  });

  cancelButton.addEventListener('click', async () => {
    show(formError);
    try {
      setBusy('正在返回任务列表…');
      await discardPendingUpload();
      window.location.replace('/task-list');
    } catch (error) {
      show(formError, error.message);
      setBusy();
    }
  });

  async function restoreUpload() {
    if (!fileId) {
      updateForm();
      return;
    }
    setBusy('正在读取已上传的文件…');
    try {
      const task = await KBRefiner.API.getStatus(fileId);
      nameInput.value = params.get('name') || task.filename.replace(/\.[^.]+$/, '');
      uploadText.textContent = task.filename;
      uploadHint.textContent = `${KBRefiner.formatBytes(task.file_size)} · 已上传`;
      if (task.status === 'awaiting_confirmation') {
        review = task.error;
      } else if (task.status === 'failed' || task.status === 'cancelled') {
        replaceRequired = true;
        show(formError, task.error || '此任务已结束，请更换文件。');
      } else if (task.status !== 'pending') {
        openTask(fileId, task.status);
        return;
      }
    } catch {
      fileId = null;
      history.replaceState(null, '', '/task-new');
      show(formError, '无法读取之前上传的文件，请重新选择文件。');
    }
    setBusy();
  }

  restoreUpload();
})();
