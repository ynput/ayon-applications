import { InputText } from '@ynput/ayon-react-components'
import styled from 'styled-components'

const INVALID_CHARS = /[\\/:*?"<>|]/

// Split filename to base name and extension (with dot)
export const splitFilename = (filename) => {
  const index = filename.lastIndexOf('.')
  if (index <= 0) return { base: filename, ext: '' }
  return { base: filename.slice(0, index), ext: filename.slice(index) }
}

export const validateBaseName = (base) => {
  const value = base.trim()
  if (!value) return 'Name cannot be empty'
  if (INVALID_CHARS.test(value)) return 'Name cannot contain any of \\ / : * ? " < > |'
  if (value.startsWith('.')) return 'Name cannot start with a dot'
  return null
}

const Row = styled.div`
  display: flex;
  align-items: center;
  gap: 4px;

  input {
    flex-grow: 1;
  }

  .ext {
    color: var(--md-sys-color-outline);
  }
`

const Message = styled.p`
  margin: 4px 0 0 0;
  color: ${({ $error }) =>
    $error ? 'var(--md-sys-color-error)' : 'var(--md-sys-color-warning, #e8b84a)'};
`

// Edits only the base name, the extension is kept
const FilenameInput = ({ base, ext, onChange, error, warning, autoFocus }) => (
  <div>
    <Row>
      <InputText
        value={base}
        autoFocus={autoFocus}
        placeholder="Icon name"
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="ext">{ext}</span>
    </Row>
    {error && <Message $error>{error}</Message>}
    {!error && warning && <Message>{warning}</Message>}
  </div>
)

export default FilenameInput
