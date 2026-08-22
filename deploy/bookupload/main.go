package main

import (
	"archive/zip"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode"
)

const (
	maxUploadBytes = 200 << 20
	maxZipFiles    = 2_000
	maxUnpacked    = 1 << 30
	libraryID      = 1
)

type server struct {
	booksRoot  string
	tmpRoot    string
	client     *http.Client
	sessionKey []byte
}

type account struct {
	Username string   `json:"username"`
	Roles    []string `json:"roles"`
}

type container struct {
	Rootfiles []struct {
		FullPath string `xml:"full-path,attr"`
	} `xml:"rootfiles>rootfile"`
}

func main() {
	keyPath := getenv("BOOKUPLOAD_SESSION_KEY", "/etc/bookupload/session.key")
	key, err := os.ReadFile(keyPath)
	if err != nil || len(key) < 32 {
		log.Fatal("session key is unavailable")
	}
	s := &server{
		booksRoot:  getenv("BOOKUPLOAD_BOOKS_ROOT", "/var/lib/kavita/books"),
		tmpRoot:    getenv("BOOKUPLOAD_TMP_ROOT", "/var/lib/bookupload/tmp"),
		client:     &http.Client{Timeout: 15 * time.Second},
		sessionKey: key,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /bookupload-menu.js", menuJS)
	mux.HandleFunc("POST /session", s.createSession)
	mux.HandleFunc("GET /", s.uploadPage)
	mux.HandleFunc("POST /api/upload", s.upload)
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		mux.ServeHTTP(w, r)
	})
	log.Fatal(http.ListenAndServe("127.0.0.1:5001", h))
}

func (s *server) health(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) }

func (s *server) createSession(w http.ResponseWriter, r *http.Request) {
	a, err := s.admin(r)
	if err != nil {
		forbidden(w)
		return
	}
	payload := fmt.Sprintf("%s|%d", a.Username, time.Now().Add(10*time.Minute).Unix())
	sig := sign(s.sessionKey, payload)
	http.SetCookie(w, &http.Cookie{Name: "bookupload_admin", Value: base64.RawURLEncoding.EncodeToString([]byte(payload + "|" + sig)), Path: "/upload/", MaxAge: 600, Secure: true, HttpOnly: true, SameSite: http.SameSiteStrictMode})
	w.WriteHeader(http.StatusNoContent)
}

func (s *server) uploadPage(w http.ResponseWriter, r *http.Request) {
	if !s.validSession(r) {
		forbidden(w)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	io.WriteString(w, uploadHTML)
}

func (s *server) upload(w http.ResponseWriter, r *http.Request) {
	if _, err := s.admin(r); err != nil {
		forbidden(w)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxUploadBytes+1)
	if err := r.ParseMultipartForm(maxUploadBytes); err != nil {
		http.Error(w, "文件超过 200 MB 或表单无效", http.StatusRequestEntityTooLarge)
		return
	}
	f, header, err := r.FormFile("epub")
	if err != nil {
		http.Error(w, "请选择 EPUB 文件", http.StatusBadRequest)
		return
	}
	defer f.Close()
	if !strings.EqualFold(filepath.Ext(header.Filename), ".epub") {
		http.Error(w, "只接受 EPUB 文件", http.StatusUnsupportedMediaType)
		return
	}
	if err := os.MkdirAll(s.tmpRoot, 0700); err != nil {
		http.Error(w, "服务器临时目录不可用", 500)
		return
	}
	tmp, err := os.CreateTemp(s.tmpRoot, "upload-*.epub")
	if err != nil {
		http.Error(w, "无法创建临时文件", 500)
		return
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err = io.Copy(tmp, io.LimitReader(f, maxUploadBytes+1)); err != nil {
		tmp.Close()
		http.Error(w, "写入上传文件失败", 500)
		return
	}
	if err = tmp.Close(); err != nil {
		http.Error(w, "写入上传文件失败", 500)
		return
	}
	title, author, err := inspectEPUB(tmpName)
	if err != nil {
		http.Error(w, "无效 EPUB："+err.Error(), http.StatusUnprocessableEntity)
		return
	}
	author = safeName(author)
	title = safeName(title)
	if author == "" || title == "" {
		http.Error(w, "EPUB 缺少有效的标题或作者", http.StatusUnprocessableEntity)
		return
	}
	dir := filepath.Join(s.booksRoot, author)
	if err = os.MkdirAll(dir, 0750); err != nil {
		http.Error(w, "无法创建书库目录", 500)
		return
	}
	dest := filepath.Join(dir, title+".epub")
	if filepath.Dir(dest) != dir {
		http.Error(w, "无效书籍路径", 400)
		return
	}
	if _, err = os.Lstat(dest); err == nil {
		http.Error(w, "同名 EPUB 已存在", http.StatusConflict)
		return
	}
	if !errors.Is(err, os.ErrNotExist) {
		http.Error(w, "无法检查目标文件", 500)
		return
	}
	if err = installExclusive(tmpName, dest); err != nil {
		if errors.Is(err, os.ErrExist) {
			http.Error(w, "同名 EPUB 已存在", http.StatusConflict)
		} else {
			http.Error(w, "保存 EPUB 失败", 500)
		}
		return
	}
	if err = s.scan(r.Header.Get("Authorization")); err != nil {
		_ = os.Remove(dest)
		http.Error(w, "Kavita 扫描未启动，文件已回滚", http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	json.NewEncoder(w).Encode(map[string]string{"title": title, "author": author})
}

func (s *server) admin(r *http.Request) (account, error) {
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") || len(auth) < 16 {
		return account{}, errors.New("no bearer token")
	}
	req, _ := http.NewRequest(http.MethodGet, "http://127.0.0.1:5000/api/account", nil)
	req.Header.Set("Authorization", auth)
	resp, err := s.client.Do(req)
	if err != nil {
		return account{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return account{}, errors.New("account rejected")
	}
	var a account
	if err = json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&a); err != nil {
		return account{}, err
	}
	for _, role := range a.Roles {
		if strings.EqualFold(role, "Admin") {
			return a, nil
		}
	}
	return account{}, errors.New("not an admin")
}

func (s *server) scan(auth string) error {
	req, _ := http.NewRequest(http.MethodPost, fmt.Sprintf("http://127.0.0.1:5000/api/library/scan?libraryId=%d&force=false", libraryID), nil)
	req.Header.Set("Authorization", auth)
	resp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("scan returned %d", resp.StatusCode)
	}
	return nil
}

func (s *server) validSession(r *http.Request) bool {
	c, err := r.Cookie("bookupload_admin")
	if err != nil {
		return false
	}
	b, err := base64.RawURLEncoding.DecodeString(c.Value)
	if err != nil {
		return false
	}
	p := strings.Split(string(b), "|")
	if len(p) != 3 {
		return false
	}
	var n int64
	if _, err = fmt.Sscan(p[1], &n); err != nil || time.Now().Unix() > n {
		return false
	}
	return hmac.Equal([]byte(p[2]), []byte(sign(s.sessionKey, p[0]+"|"+p[1])))
}

func sign(key []byte, payload string) string {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(payload))
	return base64.RawURLEncoding.EncodeToString(h.Sum(nil))
}
func forbidden(w http.ResponseWriter) {
	http.Error(w, "仅 Kavita 管理员可访问", http.StatusForbidden)
}
func getenv(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func inspectEPUB(name string) (string, string, error) {
	z, err := zip.OpenReader(name)
	if err != nil {
		return "", "", errors.New("不是 ZIP 容器")
	}
	defer z.Close()
	if len(z.File) == 0 || len(z.File) > maxZipFiles {
		return "", "", errors.New("ZIP 条目数量无效")
	}
	var total uint64
	files := map[string]*zip.File{}
	for _, f := range z.File {
		if strings.HasPrefix(f.Name, "/") || strings.Contains(f.Name, "..") {
			return "", "", errors.New("ZIP 包含不安全路径")
		}
		total += f.UncompressedSize64
		if total > maxUnpacked {
			return "", "", errors.New("EPUB 解压后过大")
		}
		files[f.Name] = f
	}
	mime, ok := files["mimetype"]
	if !ok || mime.Method != zip.Store {
		return "", "", errors.New("缺少未压缩 mimetype")
	}
	mb, err := readZip(mime, 128)
	if err != nil || strings.TrimSpace(string(mb)) != "application/epub+zip" {
		return "", "", errors.New("mimetype 不正确")
	}
	c, ok := files["META-INF/container.xml"]
	if !ok {
		return "", "", errors.New("缺少 container.xml")
	}
	cb, err := readZip(c, 1<<20)
	if err != nil {
		return "", "", errors.New("container.xml 无法读取")
	}
	var con container
	if xml.Unmarshal(cb, &con) != nil || len(con.Rootfiles) == 0 || con.Rootfiles[0].FullPath == "" {
		return "", "", errors.New("container.xml 无效")
	}
	opfName := path.Clean(con.Rootfiles[0].FullPath)
	opf, ok := files[opfName]
	if !ok {
		return "", "", errors.New("缺少 OPF")
	}
	opfB, err := readZip(opf, 4<<20)
	if err != nil {
		return "", "", errors.New("OPF 无法读取")
	}
	var d struct {
		Title   string `xml:"metadata>title"`
		Creator string `xml:"metadata>creator"`
	}
	if err := xml.Unmarshal(opfB, &d); err != nil {
		return "", "", errors.New("OPF 无效")
	}
	return strings.TrimSpace(d.Title), strings.TrimSpace(d.Creator), nil
}

func readZip(f *zip.File, max int64) ([]byte, error) {
	r, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer r.Close()
	b, err := io.ReadAll(io.LimitReader(r, max+1))
	if int64(len(b)) > max {
		return nil, errors.New("entry too large")
	}
	return b, err
}

func installExclusive(source, dest string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dest, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0640)
	if err != nil {
		return err
	}
	if _, err = io.Copy(out, in); err == nil {
		err = out.Sync()
	}
	closeErr := out.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		_ = os.Remove(dest)
		return err
	}
	return nil
}

var unsafeName = regexp.MustCompile(`[\\/:*?"<>|\x00-\x1f]`)

func safeName(s string) string {
	s = unsafeName.ReplaceAllString(strings.TrimSpace(s), " ")
	s = strings.Join(strings.FieldsFunc(s, unicode.IsSpace), " ")
	s = strings.Trim(s, ". ")
	if len([]rune(s)) > 160 {
		s = string([]rune(s)[:160])
	}
	return s
}

const uploadHTML = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>上传 EPUB</title><style>body{margin:0;background:#eee9e1;color:#2a2521;font:16px "PingFang SC","Microsoft YaHei",sans-serif}.wrap{max-width:720px;margin:72px auto;background:#f7f3ed;border:1px solid #d7cfc5;border-radius:12px;padding:32px}h1{margin-top:0}input{display:block;margin:24px 0;width:100%}button{background:#b55443;color:#fff;border:0;border-radius:6px;padding:10px 18px;font:inherit;cursor:pointer}#result{margin-top:18px;color:#716a63}</style></head><body><main class="wrap"><h1>上传 EPUB</h1><p>仅接受有效 EPUB，最大 200 MB。相同作者与书名不会覆盖已有文件。</p><form id="upload-form"><input name="epub" type="file" accept=".epub,application/epub+zip" required><button>上传并扫描会员书库</button></form><p id="result" role="status"></p></main><script>(()=>{const f=document.querySelector('#upload-form'),o=document.querySelector('#result');f.addEventListener('submit',async e=>{e.preventDefault();const u=localStorage.getItem('kavita-user');let t;try{t=JSON.parse(u).token}catch(_){}if(!t){o.textContent='Kavita 登录已失效。';return}o.textContent='正在上传…';const r=await fetch('/upload/api/upload',{method:'POST',headers:{Authorization:'Bearer '+t},body:new FormData(f)});const x=await r.text();if(!r.ok){o.textContent=x;return}const b=JSON.parse(x);o.textContent='已保存《'+b.title+'》— '+b.author+'，已请求 Kavita 扫描。';f.reset()})})()</script></body></html>`

func menuJS(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	io.WriteString(w, `(function(){'use strict';if(window.__bookuploadMenu)return;window.__bookuploadMenu=true;async function add(){let u,t;try{u=localStorage.getItem('kavita-user');t=JSON.parse(u).token}catch(_){return}if(!t)return;let r;try{r=await fetch('/api/account',{headers:{Authorization:'Bearer '+t}})}catch(_){return}if(!r.ok)return;let a;try{a=await r.json()}catch(_){return}if(!Array.isArray(a.roles)||!a.roles.some(x=>String(x).toLowerCase()==='admin'))return;if(document.getElementById('bookupload-menu-link'))return;const library=[...document.querySelectorAll('a')].find(x=>x.textContent&&x.textContent.includes('会员书库'));if(!library)return;const link=document.createElement('a');link.id='bookupload-menu-link';link.href='/upload/';link.textContent='上传 EPUB';link.style.cssText='display:block;padding:.55rem 1rem;color:inherit;text-decoration:none';link.addEventListener('click',async e=>{e.preventDefault();try{const q=await fetch('/upload/session',{method:'POST',headers:{Authorization:'Bearer '+t}});if(q.ok)location.assign('/upload/')}catch(_){}});(library.parentElement&&library.parentElement.parentElement||library.parentElement).insertAdjacentElement('afterend',link)}new MutationObserver(add).observe(document.documentElement,{childList:true,subtree:true});add()})();`)
}
