import { request } from './client'
import type { DeviceHistory, FilterOptions, GroupBy, Overview, Server, ServerCreate, ServerGroup, ServerQuery, ServerUpdate } from './types'

export const serversApi = {
  overview: () => request<Overview>('GET', '/overview'),
  list: (query: ServerQuery = {}) => request<Server[]>('GET', '/servers', { query: { ...query } }),
  grouped: (by: GroupBy, query: ServerQuery = {}) => request<ServerGroup[]>('GET', '/servers/grouped', { query: { ...query, by } }),
  get: (id: number) => request<Server>('GET', `/servers/${id}`),
  history: (id: number, hours: number) => request<DeviceHistory[]>('GET', `/servers/${id}/history`, { query: { hours } }),
  filters: () => request<FilterOptions>('GET', '/meta/filters'),
  create: (body: ServerCreate) => request<Server>('POST', '/servers', { body }),
  update: (id: number, body: ServerUpdate) => request<Server>('PATCH', `/servers/${id}`, { body }),
  remove: (id: number) => request<void>('DELETE', `/servers/${id}`),
  refresh: (id: number) => request<Server>('POST', `/servers/${id}/refresh`),
}
