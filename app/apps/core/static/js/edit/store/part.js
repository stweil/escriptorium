const partStore = {
    // need to set empty value for vue to watch them
    pk: null,
    lines: [],
    blocks: [],
    image: {},
    
    selectedTranscription: document.getElementById('document-transcriptions').value,
    
    // mutators
    load (part) {
        Object.assign(this, part);
    },
    changeTranscription(pk) {
        this.selectedTranscription = pk;
        this.fetchTranscriptions();
    },

    // helpers
    hasPrevious() { return this.loaded && this.previous !== null },
    hasNext() { return this.loaded && this.next !== null; },
    // properties
    get loaded() {
        return this.pk;
    },
    get hasMasks() {
        return this.lines.find(l=>l.mask!=null) != -1;
    },
    
    // api
    getApiRoot(pk) {
        return '/api/documents/' + DOCUMENT_ID + '/parts/' + (pk?pk:this.pk) + '/';
    },
    
    loadTranscription (transcription) {
        // use Vue.set or the transcription won't be watched.
        let line = this.lines.find(l=>l.pk == transcription.line);
        Vue.set(line, 'transcription', transcription);
    },

    // actions
    fetch(pk) {
        let uri = this.getApiRoot(pk);
        fetch(uri)
            .then((response)=>response.json())
            .then(function(data) {
                this.load(data);
                this.fetchTranscriptions();
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt fetch data!', error);
            });
    },
    
    fetchTranscriptions() {
        // first create a default transcription for every line
        this.lines.forEach(function(line) {
            this.loadTranscription({
                line: line.pk,
                transcription: this.selectedTranscription,
                content: '',
                versions: []
            });
        }.bind(this));

        //  then fetch all content page by page
        let fetchPage = function(page) {
            let uri = this.getApiRoot() + 'transcriptions/?transcription=' + this.selectedTranscription + '&page=' + page;
            fetch(uri)
                .then((response)=>response.json())
                .then(function(data) {
                    for (var i=0; i<data.results.length; i++) {
                        this.loadTranscription(data.results[i]);
                    }
                    if (data.next) fetchPage(page+1);
                }.bind(this));
        }.bind(this);
        fetchPage(1);
    },

    pushVersion(line) {
        if(!line.transcription.pk) return;
        uri = this.getApiRoot() + 'transcriptions/' + line.transcription.pk + '/new_version/';
        this.push(uri, {}, method='post')
            .then((response)=>response.json())
            .then((data) => {
                line.transcription.versions.splice(0, 0, data);
            })
            .catch(function(error) {
                console.log('couldnt save transcription state!', error);
            }.bind(this));
    },
        
    pushTranscription(lineTranscription) {
        let uri, method;
        if (lineTranscription.pk) {
            uri = this.getApiRoot() + 'transcriptions/' + lineTranscription.pk + '/';
            method = "put";
        } else {
            uri = this.getApiRoot() + 'transcriptions/';
            method = "post";
        }
        this.push(uri, lineTranscription, method=method)
            .then((response)=>response.json())
            .then((data) => {
                lineTranscription.pk = data.pk;
                lineTranscription.content = data.content;
                lineTranscription.versions = data.versions;
            })
            .catch(function(error) {
                console.log('couldnt update transcription!', error);
            }.bind(this));
    },

    createLine(line, callback) {
        let uri = this.getApiRoot() + 'lines/';
        data = {
            document_part: this.pk,
            baseline: line.baseline,
            mask: line.mask,
            block: line.region
        };
        this.push(uri, data, method="post")
            .then((response) => response.json())
            .then(function(data) {
                let newLine = data;
                newLine.transcription = {
                    line: newLine.pk,
                    transcription: this.selectedTranscription,
                    content: '',
                    versions: []
                };
                this.lines.push(newLine);
                this.recalculateOrdering();
                if (this.hasMasks) {
                    this.recalculateMasks();
                }
                callback(newLine);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt create line', error)
            });
    },
    bulkCreateLines(lines, callback) {
        let uri = this.getApiRoot() + 'lines/bulk_create/';
        let data = {lines: lines.map(l => {
            return {
                document_part: this.pk,
                baseline: l.baseline,
                mask: l.mask,
                block: l.region && l.region.pk
            };
        })};
        this.push(uri, data, method="post")
            .then((response) => response.json())
            .then(function(data) {
                let createdLines = [];
                for (let i=0; i<data.lines.length; i++) {
                    let l = data.lines[i];
                    let newLine = l;
                    newLine.transcription = {
                        line: newLine.pk,
                        transcription: this.selectedTranscription,
                        content: '',
                        versions: []
                    }
                    createdLines.push(newLine)
                    this.lines.push(newLine);
                }
                this.recalculateOrdering();
                if (this.hasMasks) {
                    this.recalculateMasks(createdLines.map(l=>l.pk));
                }
                callback(createdLines);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt create lines', error)
            });
    },
    updateLine(line, callback) {
        let uri = this.getApiRoot() + 'lines/' + line.pk + '/';
        data = {
            document_part: this.pk,
            baseline: line.baseline,
            mask: line.mask,
            block: line.region && line.region.pk
        };
        this.push(uri, data, method="put")
            .then((response) => response.json())
            .then(function(data) {
                let index = this.lines.findIndex(l=>l.pk==line.pk);
                this.lines[index].baseline = data.baseline;
                this.lines[index].mask = data.mask;
                callback(this.lines[index]);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt update line', error)
            });
    },
    bulkUpdateLines(lines, callback) {
        let uri = this.getApiRoot() + 'lines/bulk_update/';
        let data = lines.map(l => {
            return {    
                document_part: this.pk,
                pk: l.pk,
                baseline: l.baseline,
                mask: l.mask,
                block: l.region
            };
        });
        
        this.push(uri, {lines: data}, method="put")
            .then((response) => response.json())
            .then(function(data) {
                let updatedLines = [];
                for (let i=0; i<data.lines.length; i++) {
                    
                    let lineData = data.lines[i];
                    let line = this.lines.find(function(l) {
                        return l.pk==lineData.pk;
                    });
                    if (line) {
                        line.baseline = lineData.baseline;
                        line.mask = lineData.mask;
                        line.region = lineData.block;
                        updatedLines.push(line);
                    }
                }
                if (this.hasMasks) {
                    this.recalculateMasks(updatedLines.map(l=>l.pk));
                }
                if (callback) callback(updatedLines);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt update line', error)
            });
    },
    deleteLine(linePk, callback) {
        let uri = this.getApiRoot() + 'lines/' + linePk + '/';
        this.push(uri, {}, method="delete")
            .then(function(data) {
                let index = this.lines.findIndex(l=>l.pk==linePk);
                Vue.delete(this.part.lines, index);
                this.recalculateOrdering();
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt delete line #', linePk)
            });
    },
    bulkDeleteLines(pks, callback) {
        let uri = this.getApiRoot() + 'lines/bulk_delete/';
        this.push(uri, {lines: pks}, method="post")
            .then(function(data) {
                let deletedLines = [];
                for (let i=0; i<pks.length; i++) {
                    let index = this.lines.findIndex(l=>l.pk==pks[i]);
                    if(index) {
                        deletedLines.push(pks[i]);
                        Vue.delete(this.lines, index);
                    }
                }
                this.recalculateOrdering();
                if(callback) callback(deletedLines);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt bulk delete lines', error);
            });
    },
    recalculateMasks(only=[]) {
        if (!this.debouncedRecalculateMasks) {
            // avoid calling this too often
            this.debouncedRecalculateMasks = _.debounce(function(only) {
                let uri = this.getApiRoot() + 'reset_masks/';
                if (only.length >0) uri += '?only=' + only.toString();
                this.push(uri, {}, method="post")
                    .then((response) => response.json())
                    .then(function(data) {
                        for (let i=0; i<data.lines.length; i++) {
                            let lineData = data.lines[i];
                            let line = this.lines.find(function(l) {
                                return l.pk==lineData.pk;
                            });
                            if (line) {
                                line.mask = lineData.mask;
                            }
                        }
                    }.bind(this))
                    .catch(function(error) {
                        console.log('couldnt recalculate masks!', error);
                    });
            }.bind(this), 1500);
        }
        this.debouncedRecalculateMasks(only);
    },
    recalculateOrdering() {
        if (!this.debouncedRecalculateOrdering) {
            // avoid calling this too often
            this.debouncedRecalculateOrdering = _.debounce(function() {
                let uri = this.getApiRoot() + 'recalculate_ordering/';
                this.push(uri, {}, method="post")
                    .then((response) => response.json())
                    .then(function(data) {
                        for (let i=0; i<data.lines.length; i++) {
                            let lineData = data.lines[i];
                            let line = this.lines.find(function(l) {
                                return l.pk==lineData.pk;
                            });
                            if (line) {
                                line.order = i;
                            }
                        }
                    }.bind(this))
                    .catch(function(error) {
                        console.log('couldnt recalculate ordering!', error);
                    });
            }.bind(this), 3000);
        }
        this.debouncedRecalculateOrdering();
    },
    
    createRegion(region, callback) {
        let uri = this.getApiRoot() + 'blocks/';
        data = {
            document_part: this.pk,
            box: region.box
        };
        this.push(uri, data, method="post")
            .then((response) => response.json())
            .then(function(data) {
                this.blocks.push(data);
                callback(data);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt create region', error)
            });
    },
    updateRegion(region, callback) {
        let uri = this.getApiRoot() + 'blocks/' + region.pk + '/';
        data = {
            document_part: this.pk,
            box: region.box
        };
        this.push(uri, data, method="put")
            .then((response) => response.json())
            .then(function(data) {
                let index = this.blocks.findIndex(l=>l.pk==region.pk);
                this.blocks[index].box = data.box;
                callback(data);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt update region', error)
            });
    },
    deleteRegion(regionPk, callback) {
        let uri = this.getApiRoot() + 'blocks/' + regionPk + '/';
        this.push(uri, {}, method="delete")
            .then(function(data) {
                let index = this.blocks.findIndex(b=>b.pk==regionPk);
                callback(this.blocks[index].pk);
                Vue.delete(this.blocks, index);
            }.bind(this))
            .catch(function(error) {
                console.log('couldnt delete region #', regionPk)
            });
    },

    push(uri, data, method="post") {
        return fetch(uri, {
            method: method,
            credentials: "same-origin",
            headers: {
                "X-CSRFToken": Cookies.get("csrftoken"),
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });    
    },
    
    getPrevious() {
        if (this.loaded && this.previous) {
            this.fetch(this.previous);
        }
    },
    getNext() {
        if (this.loaded && this.next) {
            this.fetch(this.next);
        }
    }
};
