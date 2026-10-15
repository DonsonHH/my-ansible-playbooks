// SPDX-License-Identifier: MPL-2.0
// Kenxu's opt-in observer for pinned Xray-core. No payload inspection or UUIDs.
package kenxuflow

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/xtls/xray-core/common/buf"
	xnet "github.com/xtls/xray-core/common/net"
	"github.com/xtls/xray-core/common/session"
	"github.com/xtls/xray-core/features/stats"
)

type contextKey struct{}
type peerKey struct{}
type peerInfo struct {
	address string
	conn    net.Conn
}
type Flow struct {
	id, email, source string
	target            xnet.Destination
	outbound          string
	peerVersion       uint64
}
type Record struct {
	ID       string `json:"id"`
	Flow     string `json:"flow"`
	Email    string `json:"email"`
	At       int64  `json:"at"`
	Host     string `json:"host"`
	Port     uint16 `json:"port"`
	Network  string `json:"network"`
	Source   string `json:"source"`
	Outbound string `json:"outbound"`
	Up       int64  `json:"up"`
	Down     int64  `json:"down"`
	Closed   bool   `json:"closed"`
	Quality  string `json:"quality"`
}
type Total struct {
	Email string `json:"email"`
	Up    int64  `json:"up"`
	Down  int64  `json:"down"`
}
type Snapshot struct {
	Schema  int      `json:"schema"`
	Epoch   string   `json:"epoch"`
	Seq     uint64   `json:"seq"`
	At      int64    `json:"at"`
	Records []Record `json:"records"`
	Totals  []Total  `json:"totals"`
	More    bool     `json:"more"`
	Pending int      `json:"pending"`
	Scope   string   `json:"scope"`
	Users   []string `json:"users"`
}
type Manager struct {
	mu           sync.Mutex
	epoch        string
	seq, lastAck uint64
	rows         map[string]*Record
	totals       map[string]*Total
	frozen       *Snapshot
	frozenKeys   []string
	maxRows      int
	scope        string
	users        []string
	peers        map[net.Conn]bool
	closing      bool
}

func randomID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		panic("Kenxu observer entropy unavailable")
	}
	return hex.EncodeToString(value[:])
}
func NewManager(limit int) *Manager {
	return &Manager{epoch: randomID(), rows: make(map[string]*Record), totals: make(map[string]*Total), maxRows: limit, scope: "all-managed", users: []string{}, peers: make(map[net.Conn]bool)}
}

var global *Manager
var allow map[string]bool

func init() {
	path := os.Getenv("KENXU_FLOW_SOCKET")
	if path == "" {
		return
	}
	if !filepath.IsAbs(path) || len(path) > 100 {
		panic("Kenxu observer needs a short absolute Unix socket path")
	}
	if list := os.Getenv("KENXU_FLOW_USERS"); list != "" {
		allow = make(map[string]bool)
		for _, email := range strings.Split(list, ",") {
			allow[email] = true
		}
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		panic("Kenxu observer directory unavailable")
	}
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			panic("Kenxu observer refuses to overwrite a file")
		}
		conn, err := net.DialTimeout("unix", path, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			panic("Kenxu observer socket already in use")
		}
		if err = os.Remove(path); err != nil {
			panic("Kenxu observer stale socket unavailable")
		}
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		panic("Kenxu observer listen failed")
	}
	if err = os.Chmod(path, 0600); err != nil {
		listener.Close()
		panic("Kenxu observer socket permissions failed")
	}
	global = NewManager(65536)
	if allow != nil {
		global.scope = "selected"
		for email := range allow {
			global.users = append(global.users, email)
		}
		sort.Strings(global.users)
	}
	server := &http.Server{Handler: global.Handler(), ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 10 * time.Second}
	go server.Serve(listener)
	go func() {
		for range time.Tick(time.Minute) {
			global.redact(time.Now().UnixMilli())
		}
	}()
}
func WithPeer(ctx context.Context, conn net.Conn) context.Context {
	if global == nil || conn == nil {
		return ctx
	}
	host, _, err := net.SplitHostPort(conn.RemoteAddr().String())
	if err != nil || net.ParseIP(host) == nil {
		return ctx
	}
	in := session.InboundFromContext(ctx)
	if in != nil && in.User != nil && strings.HasPrefix(in.User.Email, "kenxu:") && (allow == nil || allow[in.User.Email]) {
		global.mu.Lock()
		closing := global.closing
		if !closing {
			global.peers[conn] = true
		}
		global.mu.Unlock()
		if closing {
			conn.Close()
		}
	}
	return context.WithValue(ctx, peerKey{}, peerInfo{address: host, conn: conn})
}
func PeerDone(ctx context.Context) {
	if global == nil {
		return
	}
	peer, ok := ctx.Value(peerKey{}).(peerInfo)
	if !ok {
		return
	}
	global.mu.Lock()
	delete(global.peers, peer.conn)
	global.mu.Unlock()
}
func Begin(ctx context.Context, target xnet.Destination) context.Context {
	if global == nil {
		return ctx
	}
	in := session.InboundFromContext(ctx)
	if in == nil || in.User == nil || !strings.HasPrefix(in.User.Email, "kenxu:") || allow != nil && !allow[in.User.Email] {
		return ctx
	}
	info, _ := ctx.Value(peerKey{}).(peerInfo)
	peer := info.address
	if peer == "" && in.Conn != nil {
		host, _, _ := net.SplitHostPort(in.Conn.RemoteAddr().String())
		peer = host
	}
	if peer == "" && in.Source.Address != nil {
		peer = in.Source.Address.String()
	}
	f := &Flow{id: randomID(), email: in.User.Email, source: peer, target: target}
	return context.WithValue(ctx, contextKey{}, f)
}
func Tracked(ctx context.Context) bool {
	f, _ := ctx.Value(contextKey{}).(*Flow)
	return global != nil && f != nil
}
func Routed(ctx context.Context, tag string) {
	f, _ := ctx.Value(contextKey{}).(*Flow)
	if f != nil && global != nil {
		global.mu.Lock()
		f.outbound = tag
		global.mu.Unlock()
	}
}
func Finish(ctx context.Context) {
	f, _ := ctx.Value(contextKey{}).(*Flow)
	if f != nil && global != nil {
		global.add(f, false, 0, f.target, true, time.Now().UnixMilli())
	}
}

type counter struct {
	base    stats.Counter
	flow    *Flow
	up      bool
	manager *Manager
}

func Wrap(ctx context.Context, base stats.Counter, up bool) stats.Counter {
	f, _ := ctx.Value(contextKey{}).(*Flow)
	if global == nil || f == nil {
		return base
	}
	return &counter{base: base, flow: f, up: up, manager: global}
}
func (c *counter) Value() int64          { return c.base.Value() }
func (c *counter) Set(value int64) int64 { return c.base.Set(value) }
func (c *counter) Add(value int64) int64 {
	result := c.base.Add(value)
	if value > 0 {
		c.manager.add(c.flow, c.up, value, c.flow.target, false, time.Now().UnixMilli())
	}
	return result
}

// XUDP can resume a logical flow on another transport. Never retain a stale
// source label or let another authenticated user reuse this user's counter.
func Rebind(c stats.Counter, ctx context.Context) bool {
	observed, ok := c.(*counter)
	if !ok {
		return true
	}
	in := session.InboundFromContext(ctx)
	if in == nil || in.User == nil || in.User.Email != observed.flow.email {
		return false
	}
	peer, ok := ctx.Value(peerKey{}).(peerInfo)
	if !ok {
		return true
	}
	observed.manager.mu.Lock()
	defer observed.manager.mu.Unlock()
	if observed.flow.source != peer.address {
		observed.flow.source = peer.address
		observed.flow.peerVersion++
	}
	return true
}

// AddBuffer preserves packet destinations for XUDP without counting the carrier.
func AddBuffer(c stats.Counter, mb buf.MultiBuffer) {
	observed, ok := c.(*counter)
	if !ok {
		c.Add(int64(mb.Len()))
		return
	}
	observed.base.Add(int64(mb.Len()))
	at := time.Now().UnixMilli()
	for _, b := range mb {
		if b == nil {
			continue
		}
		target := observed.flow.target
		if b.UDP != nil {
			target = *b.UDP
		}
		observed.manager.add(observed.flow, observed.up, int64(b.Len()), target, false, at)
	}
}
func (m *Manager) add(f *Flow, up bool, value int64, target xnet.Destination, closed bool, at int64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	bucket := at / 60000 * 60000
	host := ""
	if target.Address != nil {
		host = target.Address.String()
	}
	network := "tcp"
	if target.Network == xnet.Network_UDP {
		network = "udp"
	}
	key := f.id + "|" + strconv.FormatInt(bucket, 10) + "|" + strconv.FormatUint(f.peerVersion, 10)
	if target != f.target {
		digest := sha256.Sum256([]byte(f.id + "|" + host + "|" + strconv.Itoa(int(target.Port))))
		key += "|" + hex.EncodeToString(digest[:8])
	}
	quality := "actual"
	source := f.source
	if _, exists := m.rows[key]; !exists && len(m.rows) >= m.maxRows {
		key = "overflow|" + f.email
		quality = "unknown-capacity"
		host = ""
		source = ""
		network = "unknown"
		target.Port = 0
	}
	row := m.rows[key]
	if row == nil {
		row = &Record{ID: randomID(), Flow: f.id, Email: f.email, At: bucket, Host: host, Port: uint16(target.Port), Network: network, Source: source, Outbound: f.outbound, Quality: quality}
		m.rows[key] = row
	}
	if row.Outbound == "" {
		row.Outbound = f.outbound
	}
	if up {
		row.Up += value
	} else {
		row.Down += value
	}
	row.Closed = row.Closed || closed
	total := m.totals[f.email]
	if total == nil {
		total = &Total{Email: f.email}
		m.totals[f.email] = total
	}
	if up {
		total.Up += value
	} else {
		total.Down += value
	}
}
func (m *Manager) redact(now int64) { m.mu.Lock(); defer m.mu.Unlock(); m.redactLocked(now) }
func (m *Manager) redactLocked(now int64) {
	for _, row := range m.rows {
		if row.At < now-30*86400000 {
			row.Host = ""
			row.Source = ""
			row.Port = 0
			row.Quality = "unknown-expired"
		}
	}
	if m.frozen != nil {
		for i := range m.frozen.Records {
			row := &m.frozen.Records[i]
			if row.At < now-30*86400000 {
				row.Host = ""
				row.Source = ""
				row.Port = 0
				row.Quality = "unknown-expired"
			}
		}
	}
}
func (m *Manager) Snapshot(now int64) Snapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.redactLocked(now)
	if m.frozen != nil {
		return clone(*m.frozen)
	}
	result := Snapshot{Schema: 1, Epoch: m.epoch, At: now, Records: []Record{}, Totals: []Total{}, Pending: len(m.rows), Scope: m.scope, Users: append([]string{}, m.users...)}
	if len(m.rows) == 0 {
		return result
	}
	keys := make([]string, 0, len(m.rows))
	for key := range m.rows {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) > 256 {
		keys = keys[:256]
		result.More = true
	}
	m.seq++
	result.Seq = m.seq
	for _, key := range keys {
		result.Records = append(result.Records, *m.rows[key])
	}
	for _, t := range m.totals {
		result.Totals = append(result.Totals, *t)
	}
	sort.Slice(result.Totals, func(i, j int) bool { return result.Totals[i].Email < result.Totals[j].Email })
	m.frozen = &result
	m.frozenKeys = append([]string(nil), keys...)
	return clone(result)
}
func clone(value Snapshot) Snapshot {
	value.Records = append([]Record{}, value.Records...)
	value.Totals = append([]Total{}, value.Totals...)
	value.Users = append([]string{}, value.Users...)
	return value
}
func (m *Manager) Ack(epoch string, seq uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if epoch != m.epoch || seq == 0 {
		return errors.New("Invalid acknowledgement")
	}
	if seq <= m.lastAck {
		return nil
	}
	if m.frozen == nil || seq != m.frozen.Seq {
		return errors.New("Unknown snapshot")
	}
	// Locate by immutable flow/minute/target identity. Metadata redaction does not
	// change retained map keys, so acknowledgements retain the frozen row keys.
	for i, taken := range m.frozen.Records {
		key := m.frozenKeys[i]
		row := m.rows[key]
		if row == nil {
			panic("Kenxu observer row invariant")
		}
		row.Up -= taken.Up
		row.Down -= taken.Down
		if row.Up < 0 || row.Down < 0 {
			panic("Kenxu observer accounting invariant")
		}
		if row.Up == 0 && row.Down == 0 && row.Closed == taken.Closed {
			delete(m.rows, key)
		}
	}
	m.lastAck = seq
	m.frozen = nil
	m.frozenKeys = nil
	return nil
}
func (m *Manager) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		m.mu.Lock()
		value := Snapshot{Schema: 1, Epoch: m.epoch, At: time.Now().UnixMilli(), Scope: m.scope, Users: append([]string{}, m.users...), Records: []Record{}, Totals: []Total{}, Pending: len(m.rows)}
		for _, t := range m.totals {
			value.Totals = append(value.Totals, *t)
		}
		m.mu.Unlock()
		sort.Slice(value.Totals, func(i, j int) bool { return value.Totals[i].Email < value.Totals[j].Email })
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		json.NewEncoder(w).Encode(value)
	})
	mux.HandleFunc("/snapshot", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		json.NewEncoder(w).Encode(m.Snapshot(time.Now().UnixMilli()))
	})
	mux.HandleFunc("/ack", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		var v struct {
			Epoch string `json:"epoch"`
			Seq   uint64 `json:"seq"`
		}
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&v) != nil || m.Ack(v.Epoch, v.Seq) != nil {
			w.WriteHeader(409)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte("{\"ok\":true}"))
	})
	return mux
}

// Checkpoint persists the same immutable batches used by the live collector.
// A crash after fsync but before Ack produces a duplicate, never new billing.
func (m *Manager) Checkpoint(directory string) error {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	for {
		snapshot := m.Snapshot(time.Now().UnixMilli())
		if len(snapshot.Records) == 0 {
			return nil
		}
		raw, err := json.Marshal(snapshot)
		if err != nil {
			return err
		}
		file, err := os.CreateTemp(directory, ".flow-")
		if err != nil {
			return err
		}
		name := file.Name()
		if err = file.Chmod(0600); err == nil {
			_, err = file.Write(raw)
		}
		if err == nil {
			err = file.Sync()
		}
		closeErr := file.Close()
		if err == nil {
			err = closeErr
		}
		if err != nil {
			os.Remove(name)
			return err
		}
		destination := filepath.Join(directory, snapshot.Epoch+"-"+strconv.FormatUint(snapshot.Seq, 10)+".json")
		if err = os.Rename(name, destination); err != nil {
			os.Remove(name)
			return err
		}
		dir, err := os.Open(directory)
		if err != nil {
			return err
		}
		err = dir.Sync()
		dir.Close()
		if err != nil {
			return err
		}
		if err = m.Ack(snapshot.Epoch, snapshot.Seq); err != nil {
			return err
		}
	}
}

// Called after Xray closes its listeners/handlers. Allow the bounded splice
// checkpoint to finish, then persist the tail. SIGKILL cannot run this hook.
func Shutdown() error {
	if global == nil {
		return nil
	}
	directory := filepath.Join(filepath.Dir(os.Getenv("KENXU_FLOW_SOCKET")), "spool")
	global.mu.Lock()
	global.closing = true
	peers := make([]net.Conn, 0, len(global.peers))
	for conn := range global.peers {
		peers = append(peers, conn)
	}
	global.mu.Unlock()
	for _, conn := range peers {
		conn.Close()
	}
	time.Sleep(1200 * time.Millisecond)
	return global.Checkpoint(directory)
}
