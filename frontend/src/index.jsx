import axios from 'axios'
import React, { useContext, useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { AddonProvider, AddonContext } from '@ynput/ayon-react-addon-provider'

import '@ynput/ayon-react-components/dist/style.css'

import CustomIcons from './CustomIcons'

const ADDON_NAME = 'applications'

const App = () => {
  const context = useContext(AddonContext)
  const accessToken = context?.accessToken
  const addonVersion = context?.addonVersion
  const [tokenSet, setTokenSet] = useState(false)

  useEffect(() => {
    if (accessToken && !tokenSet) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${accessToken}`
      setTokenSet(true)
    }
  }, [accessToken, tokenSet])

  if (!tokenSet || !addonVersion) {
    return null
  }

  const baseUrl = `/api/addons/${ADDON_NAME}/${addonVersion}`
  return <CustomIcons baseUrl={baseUrl} />
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AddonProvider
      devConfig={{
        addonName: ADDON_NAME,
        addonVersion: import.meta.env.VITE_ADDON_VERSION,
        scope: 'settings',
      }}
    >
      <App />
    </AddonProvider>
  </React.StrictMode>,
)
