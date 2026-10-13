import axios from 'axios'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Button,
  Dialog,
  Icon,
  InputText,
  Panel,
  ScrollPanel,
  Spacer,
  Toolbar,
} from '@ynput/ayon-react-components'
import styled, { css } from 'styled-components'

import ImageCropDialog, { isCroppable, loadImage, needsCrop } from './ImageCropDialog'

const ALLOWED_EXTENSIONS = ['png', 'jpg', 'jpeg', 'svg', 'gif', 'webp', 'ico']

const isAllowedFile = (file) =>
  ALLOWED_EXTENSIONS.includes(file.name.split('.').pop().toLowerCase())

const getErrorMessage = (error) => {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return error?.message || 'Unknown error'
}

const getIconUrl = (baseUrl, filename) =>
  `${baseUrl}/customIcons/${encodeURIComponent(filename)}`

const Container = styled.div`
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  padding: 8px;
`

const MainPanel = styled(Panel)`
  flex-grow: 1;
  min-height: 0;
  gap: 8px;

  ${({ $dragging }) =>
    $dragging &&
    css`
      outline: 2px dashed var(--md-sys-color-primary);
      outline-offset: -4px;
    `}
`

const Title = styled.h2`
  margin: 0;
  font-size: 16px;
`

const Hint = styled.p`
  margin: 0;
  color: var(--md-sys-color-outline);
`

const Message = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 4px 4px 8px;
  border-radius: 4px;
  background-color: ${({ $error }) =>
    $error
      ? 'var(--md-sys-color-error-container)'
      : 'var(--md-sys-color-primary-container)'};
  color: ${({ $error }) =>
    $error
      ? 'var(--md-sys-color-on-error-container)'
      : 'var(--md-sys-color-on-primary-container)'};

  .text {
    flex-grow: 1;
  }
`

const Grid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
`

const Card = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 16px 8px 8px 8px;
  border-radius: 4px;
  background-color: var(--md-sys-color-surface-container-high);
  position: relative;

  img {
    width: 64px;
    height: 64px;
    object-fit: contain;
  }

  .filename {
    width: 100%;
    text-align: center;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .actions {
    position: absolute;
    top: 4px;
    right: 4px;
    display: flex;
    opacity: 0;
  }

  &:hover .actions,
  .actions:focus-within {
    opacity: 1;
  }
`

const Empty = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 200px;
  color: var(--md-sys-color-outline);

  .icon {
    font-size: 48px;
  }
`

const CustomIcons = ({ baseUrl }) => {
  const [icons, setIcons] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [message, setMessage] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  // Changes after each upload so replaced icons are not shown from cache
  const [cacheKey, setCacheKey] = useState(() => Date.now())
  // Files waiting for confirmation because they replace existing icons
  const [pendingFiles, setPendingFiles] = useState(null)
  const [toDelete, setToDelete] = useState(null)
  // Image waiting in crop dialog, holds 'resolve' of the promise returned by 'requestCrop'
  const [cropRequest, setCropRequest] = useState(null)
  const fileInputRef = useRef(null)
  const dragCounter = useRef(0)

  const loadIcons = useCallback(async () => {
    try {
      const response = await axios.get(`${baseUrl}/customIcons`)
      const filenames = response.data.icons.map((icon) => icon.filename)
      filenames.sort((a, b) => a.localeCompare(b))
      setIcons(filenames)
    } catch (error) {
      setMessage({ error: true, text: `Failed to load icons: ${getErrorMessage(error)}` })
    } finally {
      setLoading(false)
    }
  }, [baseUrl])

  useEffect(() => {
    loadIcons()
  }, [loadIcons])

  const filteredIcons = useMemo(() => {
    const value = search.trim().toLowerCase()
    if (!value) return icons
    return icons.filter((filename) => filename.toLowerCase().includes(value))
  }, [icons, search])

  const uploadFiles = async (files) => {
    setUploading(true)
    const failed = []
    for (const file of files) {
      try {
        await axios.put(getIconUrl(baseUrl, file.name), file, {
          headers: { 'Content-Type': file.type || 'application/octet-stream' },
        })
      } catch (error) {
        failed.push(`${file.name} (${getErrorMessage(error)})`)
      }
    }
    setUploading(false)
    setCacheKey(Date.now())
    await loadIcons()

    if (failed.length) {
      setMessage({ error: true, text: `Failed to upload ${failed.join(', ')}` })
    } else {
      const count = files.length
      setMessage({ error: false, text: `Uploaded ${count} icon${count === 1 ? '' : 's'}` })
    }
  }

  const requestCrop = (request) =>
    new Promise((resolve) => setCropRequest({ ...request, resolve }))

  const onCropDone = (result) => {
    cropRequest.resolve(result)
    setCropRequest(null)
  }

  // Offer cropping for images that are not square or are bigger than the max size
  const prepareFile = async (file) => {
    if (!isCroppable(file.name)) return file
    let loaded
    try {
      loaded = await loadImage(file)
    } catch {
      // Let the server decide what to do with it
      return file
    }
    if (!needsCrop(loaded.image)) {
      URL.revokeObjectURL(loaded.url)
      return file
    }
    return requestCrop({ file, ...loaded, allowOriginal: true })
  }

  const handleFiles = async (fileList) => {
    const files = Array.from(fileList || [])
    if (!files.length) return

    const valid = files.filter(isAllowedFile)
    const invalid = files.filter((file) => !isAllowedFile(file))
    if (invalid.length) {
      setMessage({
        error: true,
        text:
          `Skipped unsupported files: ${invalid.map((file) => file.name).join(', ')}. ` +
          `Allowed extensions: ${ALLOWED_EXTENSIONS.join(', ')}`,
      })
    }
    if (!valid.length) return

    const prepared = []
    for (const file of valid) {
      const result = await prepareFile(file)
      if (result) prepared.push(result)
    }
    if (!prepared.length) return

    if (prepared.some((file) => icons.includes(file.name))) {
      setPendingFiles(prepared)
      return
    }
    uploadFiles(prepared)
  }

  // Crop an already uploaded icon and replace it
  const onCropExisting = async (filename) => {
    let loaded
    let file
    try {
      const response = await axios.get(getIconUrl(baseUrl, filename), {
        params: { v: cacheKey },
        responseType: 'blob',
      })
      file = new File([response.data], filename, { type: response.data.type })
      loaded = await loadImage(file)
    } catch (error) {
      setMessage({ error: true, text: `Failed to load ${filename}: ${getErrorMessage(error)}` })
      return
    }
    const result = await requestCrop({ file, ...loaded, allowOriginal: false })
    if (!result) return
    if (result.name !== filename && icons.includes(result.name)) {
      setPendingFiles([result])
      return
    }
    uploadFiles([result])
  }

  const onInputChange = (event) => {
    handleFiles(event.target.files)
    // Allow selecting the same file again
    event.target.value = ''
  }

  const onConfirmReplace = () => {
    const files = pendingFiles
    setPendingFiles(null)
    uploadFiles(files)
  }

  const onConfirmDelete = async () => {
    const filename = toDelete
    setToDelete(null)
    try {
      await axios.delete(getIconUrl(baseUrl, filename))
      setMessage({ error: false, text: `Deleted ${filename}` })
    } catch (error) {
      setMessage({ error: true, text: `Failed to delete ${filename}: ${getErrorMessage(error)}` })
    }
    await loadIcons()
  }

  const onDragEnter = (event) => {
    if (!event.dataTransfer?.types?.includes('Files')) return
    event.preventDefault()
    dragCounter.current += 1
    setDragging(true)
  }

  const onDragLeave = (event) => {
    event.preventDefault()
    dragCounter.current = Math.max(0, dragCounter.current - 1)
    if (dragCounter.current === 0) setDragging(false)
  }

  const onDrop = (event) => {
    event.preventDefault()
    dragCounter.current = 0
    setDragging(false)
    if (!uploading && !cropRequest) handleFiles(event.dataTransfer.files)
  }

  const replacedFilenames = (pendingFiles || [])
    .map((file) => file.name)
    .filter((filename) => icons.includes(filename))

  return (
    <Container>
      <MainPanel
        $dragging={dragging}
        onDragEnter={onDragEnter}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <Toolbar>
          <Title>Custom icons</Title>
          <Spacer />
          <InputText
            placeholder="Filter icons..."
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <Button icon="refresh" tooltip="Reload" onClick={loadIcons} />
          <Button
            icon="upload"
            label={uploading ? 'Uploading...' : 'Upload icons'}
            variant="filled"
            disabled={uploading || !!cropRequest}
            onClick={() => fileInputRef.current?.click()}
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ALLOWED_EXTENSIONS.map((ext) => `.${ext}`).join(',')}
            style={{ display: 'none' }}
            onChange={onInputChange}
          />
        </Toolbar>
        <Hint>
          Uploaded icons can be selected in the Icon field of application and tool variants.
          Drop image files here to upload them. Images that are not square or are bigger than
          512×512 px can be cropped and resized before upload. Uploading a file with an existing
          name replaces it.
        </Hint>

        {message && (
          <Message $error={message.error}>
            <Icon icon={message.error ? 'error' : 'check_circle'} />
            <span className="text">{message.text}</span>
            <Button icon="close" variant="text" onClick={() => setMessage(null)} />
          </Message>
        )}

        <ScrollPanel style={{ flexGrow: 1, background: 'transparent' }}>
          {!loading && !filteredIcons.length ? (
            <Empty>
              <Icon icon="imagesmode" className="icon" />
              {icons.length ? 'No icons match the filter' : 'No custom icons uploaded yet'}
            </Empty>
          ) : (
            <Grid>
              {filteredIcons.map((filename) => (
                <Card key={filename} title={filename}>
                  <img src={`${getIconUrl(baseUrl, filename)}?v=${cacheKey}`} alt={filename} />
                  <span className="filename">{filename}</span>
                  <div className="actions">
                    {isCroppable(filename) && (
                      <Button
                        icon="crop"
                        variant="text"
                        tooltip="Crop and resize icon"
                        disabled={uploading}
                        onClick={() => onCropExisting(filename)}
                      />
                    )}
                    <Button
                      icon="delete"
                      variant="text"
                      tooltip="Delete icon"
                      onClick={() => setToDelete(filename)}
                    />
                  </div>
                </Card>
              ))}
            </Grid>
          )}
        </ScrollPanel>
      </MainPanel>

      {cropRequest && (
        <ImageCropDialog key={cropRequest.url} request={cropRequest} onDone={onCropDone} />
      )}

      <Dialog
        isOpen={!!pendingFiles}
        onClose={() => setPendingFiles(null)}
        header="Replace existing icons?"
        size="sm"
        footer={<Button label="Replace" variant="filled" onClick={onConfirmReplace} />}
      >
        <p>These icons already exist and will be replaced:</p>
        <ul>
          {replacedFilenames.map((filename) => (
            <li key={filename}>{filename}</li>
          ))}
        </ul>
      </Dialog>

      <Dialog
        isOpen={!!toDelete}
        onClose={() => setToDelete(null)}
        header="Delete icon?"
        size="sm"
        footer={<Button label="Delete" variant="danger" onClick={onConfirmDelete} />}
      >
        <p>
          Delete <b>{toDelete}</b>? Applications and tools that use it will have no icon.
        </p>
      </Dialog>
    </Container>
  )
}

export default CustomIcons
